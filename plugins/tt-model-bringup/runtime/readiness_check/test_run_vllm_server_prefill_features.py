# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

import json

import pytest

from readiness_check import run_vllm_server
from readiness_check.run_vllm_server import (
    ALL_STAGES,
    DEFAULT_STAGES,
    PREFIX_CACHE_THRESHOLDS,
    _attach_serial_reference,
    _counter_delta,
    _parse_prometheus_counters,
    _parse_stages,
    _prefix_cache_verdict,
    _server_command,
)

METRICS = """
# HELP vllm:prefix_cache_queries_total Prefix cache queries, in terms of number of queried tokens.
# TYPE vllm:prefix_cache_queries_total counter
vllm:prefix_cache_queries_total{engine="0",model_name="org/model"} 4096.0
vllm:prefix_cache_hits_total{engine="0",model_name="org/model"} 2048.0
vllm:external_prefix_cache_queries_total{engine="0",model_name="org/model"} 99.0
vllm:num_requests_running{engine="0",model_name="org/model"} 1.0
"""


def test_new_stages_are_opt_in():
    assert "prefix_cache" in ALL_STAGES
    assert "prefill_burst" in ALL_STAGES
    assert "prefix_cache" not in DEFAULT_STAGES
    assert _parse_stages("serve,prefix_cache,prefill_burst") == ["serve", "prefix_cache", "prefill_burst"]


@pytest.mark.parametrize(
    ("flag", "expected"),
    [(None, []), (True, ["--enable-prefix-caching"]), (False, ["--no-enable-prefix-caching"])],
)
def test_server_command_records_prefix_caching_flag(flag, expected):
    cmd = _server_command(
        hf_model="org/model",
        max_num_seqs=32,
        block_size=64,
        port=8000,
        max_model_len=4096,
        tt_config={"sample_on_device_mode": "all"},
        additional_args=["--async-scheduling"],
        enable_prefix_caching=flag,
    )
    present = [c for c in cmd if "prefix-caching" in c]
    assert present == expected
    assert cmd[-1] == "--async-scheduling"
    assert cmd[cmd.index("--max_model_len") + 1] == "4096"


def test_prometheus_counters_sum_samples_and_fold_total_suffix():
    counters = _parse_prometheus_counters(METRICS)
    assert counters["vllm:prefix_cache_queries"] == 4096.0
    assert counters["vllm:prefix_cache_hits"] == 2048.0
    assert counters["vllm:external_prefix_cache_queries"] == 99.0
    delta = _counter_delta(
        {"vllm:prefix_cache_queries": 4000.0, "vllm:prefix_cache_hits": 2000.0},
        {"vllm:prefix_cache_queries": 4096.0, "vllm:prefix_cache_hits": 2048.0},
    )
    assert delta == {"prefix_cache_queries_delta": 96.0, "prefix_cache_hits_delta": 48.0, "hit_rate": 0.5}


def _request(label, kind, *, ttft, hit_rate, tokens, logprob):
    return {
        "label": label,
        "kind": kind,
        "ttft_ms": ttft,
        "hit_rate": hit_rate,
        "prefix_cache_queries_delta": 2300.0,
        "prefix_cache_hits_delta": 2300.0 * (hit_rate or 0.0),
        "tokens": list(tokens),
        "token_logprobs": [logprob] + [-0.5] * (len(tokens) - 1),
    }


def _record(**overrides):
    tokens = [f"t{i}" for i in range(64)]
    record = {
        "apc_log_enabled": True,
        "requests": [
            _request("cold1", "cold", ttft=1000.0, hit_rate=0.0, tokens=tokens, logprob=-1.0),
            _request("warm1", "warm", ttft=200.0, hit_rate=0.98, tokens=tokens, logprob=-1.1),
            _request("warm2", "warm", ttft=180.0, hit_rate=0.98, tokens=tokens, logprob=-1.05),
        ],
        "shared_prefix_bench": {"hit_rate": 0.82},
    }
    record.update(overrides)
    return record


def test_prefix_cache_verdict_passes_on_hits_fast_ttft_and_identical_output():
    record = _record()
    status, reasons = _prefix_cache_verdict(record, PREFIX_CACHE_THRESHOLDS)
    assert (status, reasons) == ("pass", [])
    assert record["ttft_ratio"] == pytest.approx(0.2)
    assert record["requests"][1]["greedy_identical_through"] == 64


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r.update(apc_log_enabled=False), "prefix caching disabled"),
        (lambda r: r["requests"][1].update(hit_rate=0.2), "warm request hit rate"),
        (lambda r: r["requests"][0].update(hit_rate=0.5), "cold request hit rate"),
        (lambda r: [req.update(ttft_ms=900.0) for req in r["requests"][1:]], "TTFT ratio"),
        (lambda r: r["requests"][1]["tokens"].__setitem__(0, "other"), "first token differs"),
        (lambda r: r["requests"][1]["token_logprobs"].__setitem__(0, -3.0), "logprob delta"),
        (lambda r: r["requests"][2]["tokens"].__setitem__(5, "other"), "diverges after 5 tokens"),
        (lambda r: r.update(shared_prefix_bench={"hit_rate": 0.3}), "shared-prefix benchmark hit rate"),
    ],
)
def test_prefix_cache_verdict_fails_each_gate(mutate, message):
    record = _record()
    mutate(record)
    status, reasons = _prefix_cache_verdict(record, PREFIX_CACHE_THRESHOLDS)
    assert status == "fail"
    assert any(message in reason for reason in reasons), reasons


def _with_long_resume(record, *, warm_hit=0.9, cold_hit=0.0, diverge_at=None, first_token="t0"):
    tokens = [f"t{i}" for i in range(64)]
    warm_tokens = list(tokens)
    warm_tokens[0] = first_token
    if diverge_at is not None:
        warm_tokens[diverge_at] = "x"
    record["requests"] += [
        _request("resume_warm", "resume_warm", ttft=300.0, hit_rate=warm_hit, tokens=warm_tokens, logprob=-1.0),
        _request("resume_cold", "resume_cold", ttft=1200.0, hit_rate=cold_hit, tokens=tokens, logprob=-1.0),
    ]
    record["long_resume"] = {"checked": True, "continuation_tokens": 256}
    return record


def test_long_resume_pair_passes_when_the_resumed_output_matches_the_cold_reference():
    record = _with_long_resume(_record())
    status, reasons = _prefix_cache_verdict(record, PREFIX_CACHE_THRESHOLDS)
    assert (status, reasons) == ("pass", [])
    assert record["requests"][-2]["greedy_identical_through"] == 64


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"warm_hit": 0.2}, "not served from the cache"),
        ({"cold_hit": 0.9}, "reference still hit the cache"),
        ({"first_token": "other"}, "first token differs between the resumed"),
        ({"diverge_at": 4}, "long resume: greedy output diverges after 4"),
    ],
)
def test_long_resume_pair_fails_each_gate(kwargs, message):
    record = _with_long_resume(_record(), **kwargs)
    status, reasons = _prefix_cache_verdict(record, PREFIX_CACHE_THRESHOLDS)
    assert status == "fail"
    assert any(message in reason for reason in reasons), reasons


class _WordTokenizer:
    def encode(self, text):
        return text.split()

    def decode(self, token_ids, skip_special_tokens=True):
        return " ".join(token_ids)


def test_resume_suffix_differs_from_the_prefix(tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("alpha beta gamma\n\ndelta epsilon zeta\n\neta theta iota\n")
    tokenizer = _WordTokenizer()
    prompt, count = run_vllm_server._build_prefix_cache_prompt(tokenizer, prompts, 5)
    suffix = run_vllm_server._build_resume_suffix(tokenizer, prompts, 4)
    assert count == 5
    assert prompt.split()[:3] == ["alpha", "beta", "gamma"]
    assert suffix.split()[:3] == ["eta", "theta", "iota"]
    assert len(suffix.split()) == 4


def test_prefix_cache_verdict_tolerates_bf16_divergence_after_the_hard_window():
    record = _record()
    record["requests"][1]["tokens"][40] = "drift"
    status, reasons = _prefix_cache_verdict(record, PREFIX_CACHE_THRESHOLDS)
    assert status == "pass", reasons
    assert record["requests"][1]["greedy_identical_through"] == 40


def test_attach_serial_reference_records_ttft_ratios_and_config_match(tmp_path):
    config = {"prompt_len": 128, "output_len": 32, "num_requests": 32, "concurrency": 32}
    serial = {"ttft_ms": {"p50": 2000.0, "p99": 5000.0, "mean": 2500.0}, "config": config}
    summary_file = tmp_path / "burst.json"
    summary = {"ttft_ms": {"p50": 1000.0, "p99": 2000.0, "mean": 1000.0}, "config": dict(config)}
    result = _attach_serial_reference(summary_file, summary, serial, tmp_path / "serial.json")
    assert result["serial_reference"]["ttft_ratio_p99"] == pytest.approx(0.4)
    assert result["serial_reference"]["ttft_ratio_mean"] == pytest.approx(0.4)
    assert result["serial_reference"]["config_matches"] is True
    assert json.loads(summary_file.read_text())["serial_reference"]["ttft_ratio_p50"] == pytest.approx(0.5)
    mismatched = _attach_serial_reference(summary_file, {"ttft_ms": {}, "config": {**config, "concurrency": 8}}, serial, None)
    assert mismatched["serial_reference"]["config_matches"] is False
    assert _attach_serial_reference(summary_file, {"ttft_ms": {}}, None, None) == {"ttft_ms": {}}


def test_run_summary_merges_hold_open_and_attached_runs(tmp_path):
    path = tmp_path / "vllm_run_summary.json"
    run_vllm_server._write_run_summary(
        path, {"stages": ["serve"], "server_command": ["vllm"], "enable_prefix_caching": True, "outputs": {}}
    )
    run_vllm_server._write_run_summary(
        path,
        {"stages": ["prefix_cache"], "server_command": None, "enable_prefix_caching": None, "outputs": {"prefix_cache": "p.json"}},
    )
    merged = json.loads(path.read_text())
    assert merged["server_command"] == ["vllm"]
    assert merged["enable_prefix_caching"] is True
    assert merged["stages"] == ["prefix_cache", "serve"]
    assert merged["outputs"] == {"prefix_cache": "p.json"}


def test_module_constants_name_the_counters_the_check_reads():
    assert run_vllm_server.PREFIX_CACHE_COUNTERS == ("vllm:prefix_cache_queries", "vllm:prefix_cache_hits")
