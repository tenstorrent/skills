# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
Run the vLLM server readiness check.

A single entry point that can launch the server, run any subset of checks
against it, or attach to an already-running server. Pick what to do via
``--stages``.

Stages:
  serve         Launch the vLLM server. If it's the only stage, hold it open
                until SIGINT/SIGTERM; otherwise shut it down once the trailing
                checks finish. Requires ``--mesh-device``.
  sampling      Run the canonical pytest plugin sampling tests against the
                live server. ``--sampling-profile`` selects full vs smoke.
  qualitative   Run qualitative prompts and save completions for manual
                review.
  benchmark     Run ``vllm bench serve``. The primary headline profile is a
                greedy single-user 128/128/1 decode run that reports TTFT
                separately from TPOT/ITL and writes raw ``vllm_result.json``
                plus normalized ``vllm_benchmark.json``. By default the runner
                also runs the vLLM-nightly-shaped 100/100/32 serving-burst
                profile as a secondary CI/capacity artifact. Defaults to greedy
                ``--temperature 0.0``; opt into exact server-generation-config
                behavior with ``--benchmark-use-server-generation-config``.
  prefix_cache  Send one 2305-token prompt cold, then warm, reset the prefix
                cache, and repeat; then append a 256-token new suffix so the
                resume is a long continuation and compare it with a cold run
                after another reset (needs VLLM_SERVER_DEV_MODE=1 on the server
                for /reset_prefix_cache). Records /metrics prefix-cache counter
                deltas, TTFT, first-token logprobs and greedy output per
                request, runs the shared-prefix ``vllm bench serve`` workload,
                and writes ``prefix_cache_check.json`` with a pass/fail verdict.
  prefill_burst Run 32 x 128-token prompts at concurrency 32 so the scheduler
                forms multi-row prefill steps; writes
                ``vllm_prefill_burst_benchmark.json``. Pass
                ``--prefill-burst-serial-result`` to record TTFT ratios against
                the same workload measured with batched prefill disabled.

Every run writes ``vllm_run_summary.json`` with the server command, the
prefix-caching flag, and the prefix-caching state read from the server log.

Default: ``--stages serve,sampling,qualitative,benchmark`` (launch, run all
checks, shut down). Full launch with the typical tuning flags:

    python -m readiness_check.run_vllm_server \\
        --model-dir models/autoports/<model_name> \\
        --hf-model <hf-model-id> \\
        --mesh-device N150 \\
        --max-num-seqs 32 \\
        --max-model-len 32768 \\
        --tt-config '{"trace_region_size": 85000000, "fabric_config": "FABRIC_1D"}'

To hold the server open without running checks (skip trace compile on
subsequent check invocations):

    python -m readiness_check.run_vllm_server \\
        --stages serve \\
        --model-dir models/autoports/<model_name> \\
        --hf-model <hf-model-id> \\
        --mesh-device N150 \\
        --max-model-len 32768 \\
        --tt-config '{"trace_region_size": 85000000, "fabric_config": "FABRIC_1D"}'

To run a single check against the running server, pass ``--server-url`` and
omit ``serve`` from the stages:

    python -m readiness_check.run_vllm_server \\
        --stages sampling \\
        --max-num-seqs 32 \\
        --sampling-profile smoke \\
        --server-url http://localhost:8000 \\
        --model-dir models/autoports/<model_name> \\
        --hf-model <hf-model-id>

To install vLLM, consult `tenstorrent/vllm-tt-plugin` checkout's
current README and installation script. Use the upstream vLLM version it
recommends and follow the documented plugin installation procedure.
Set VLLM_TT_PLUGIN_ROOT to the selected plugin checkout.
Record the selected pair; do not use the old tenstorrent/vllm fork.

Before invoking it, two things must already be true:

  1. `<model_dir>/tt/generator_vllm.py` exists and implements the model.
  2. The model architecture is registered in the TT vLLM platform registry.
     Without that registration vLLM will reject the architecture at startup with
     "architecture not in TT registry".

The launch command and env vars match `.github/workflows/vllm-nightly-tests-impl.yaml`.

On-device sampling is enforced (`sample_on_device_mode: all` in the plugin
config). A model that cannot serve sampling from the device is not
production-ready, so the readiness check fails fast there rather than papering
over it with host-side sampling.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

import openai
import requests
from transformers import AutoTokenizer

from readiness_check.mesh_device import MESH_SHAPES

DEFAULT_PORT = 8000
DEFAULT_BLOCK_SIZE = 64
DEFAULT_MAX_NUM_SEQS = 32
# TT trace compile + weight load on first start can take 10+ minutes.
DEFAULT_SERVER_TIMEOUT_S = 1200
# Matches the nightly CI workflow.
DEFAULT_VLLM_RPC_TIMEOUT_MS = 300000

STAGE_SERVE = "serve"
STAGE_SAMPLING = "sampling"
STAGE_QUALITATIVE = "qualitative"
STAGE_BENCHMARK = "benchmark"
STAGE_PREFIX_CACHE = "prefix_cache"
STAGE_PREFILL_BURST = "prefill_burst"
DEFAULT_STAGES: tuple[str, ...] = (STAGE_SERVE, STAGE_SAMPLING, STAGE_QUALITATIVE, STAGE_BENCHMARK)
ALL_STAGES: tuple[str, ...] = (*DEFAULT_STAGES, STAGE_PREFIX_CACHE, STAGE_PREFILL_BURST)

# Prefix-cache check: one shared prompt sent cold, then warm, with counter
# deltas from /metrics around every request.
DEFAULT_PREFIX_CACHE_PROMPT_TOKENS = 2305
DEFAULT_PREFIX_CACHE_OUTPUT_TOKENS = 64
DEFAULT_PREFIX_CACHE_RESUME_SUFFIX_TOKENS = 256
PREFIX_CACHE_THRESHOLDS: dict[str, float] = {
    "warm_hit_rate_min": 0.9,
    "cold_hit_rate_max": 0.05,
    "ttft_ratio_max": 0.5,
    "first_token_logprob_delta_max": 0.25,
    "greedy_identical_min_tokens": 16,
    "shared_prefix_hit_rate_min": 0.7,
}
APC_ENABLED_LOG_LINE = "Automatic prefix caching is enabled"
APC_DISABLED_LOG_LINE = "Automatic prefix caching is disabled"
PREFIX_CACHE_COUNTERS = ("vllm:prefix_cache_queries", "vllm:prefix_cache_hits")
SHARED_PREFIX_BENCH_ARGS: tuple[str, ...] = (
    "--dataset-name",
    "prefix_repetition",
    "--prefix-repetition-prefix-len",
    "2048",
    "--prefix-repetition-suffix-len",
    "128",
    "--prefix-repetition-num-prefixes",
    "4",
    "--prefix-repetition-output-len",
    "32",
)

# Prefill burst: many short prompts at once so the scheduler forms multi-row
# prefill steps. Compared against a serial-prefill run of the same shape.
DEFAULT_BURST_PROMPT_LEN = 128
DEFAULT_BURST_OUTPUT_LEN = 32
DEFAULT_BURST_NUM_REQUESTS = 32
DEFAULT_BURST_CONCURRENCY = 32

# Primary benchmark shape: single-user decode, used for headline t/s/u.
# We force greedy temperature for readiness t/s/u unless the caller explicitly
# asks to reproduce exact server-generation-config behavior.
DEFAULT_BENCH_PROMPT_LEN = 128
DEFAULT_BENCH_OUTPUT_LEN = 128
DEFAULT_BENCH_NUM_REQUESTS = 1
DEFAULT_BENCH_CONCURRENCY: Optional[int] = 1
DEFAULT_BENCH_TEMPERATURE = 0.0

# Secondary serving-burst profile matching `.github/workflows/vllm-nightly-tests-impl.yaml`.
DEFAULT_CI_SERVING_BENCHMARK = True
DEFAULT_CI_BENCH_PROMPT_LEN = 100
DEFAULT_CI_BENCH_OUTPUT_LEN = 100
DEFAULT_CI_BENCH_NUM_REQUESTS = 32
DEFAULT_CI_BENCH_CONCURRENCY: Optional[int] = None

SAMPLING_PROFILE_FULL = "full"
SAMPLING_PROFILE_SMOKE = "smoke"
DEFAULT_SAMPLING_PROFILE = SAMPLING_PROFILE_FULL
_SMOKE_SAMPLING_TESTS: tuple[str, ...] = (
    "test_request_isolation.py::TestBatchIsolation::test_mixed_params_batch",
    "test_seeding_and_variety.py::TestSeedingAndVariety::test_top1_is_greedy",
    "test_logprobs.py::TestLogprobs::test_chat_logprobs_all_vocab",
    "test_host_only_params.py::TestHostOnlyParameters::test_min_p",
)

# Always enforce on-device sampling. A ported model that cannot serve sampling
# from the device is not production-ready; the readiness check fails fast here
# rather than papering over it with host-side sampling. Users override by
# passing `--tt-config '{"sample_on_device_mode": "..."}'` if they need to.
DEFAULT_TT_CONFIG: dict[str, Any] = {"sample_on_device_mode": "all"}

# Fast-fail markers cribbed from the nightly CI workflow. vLLM writes one of
# these to the server log within seconds of an engine-core crash; polling
# /health past that point just burns time.
_FATAL_LOG_PATTERNS = (
    "EngineCore failed to start",
    "EngineCore encountered a fatal error",
    "EngineDeadError",
    "Engine core initialization failed",
    "Failed core proc",
)



def _reject_legacy_plugin_source(origin: Path) -> None:
    root = origin.parent.parent.parent
    if (
        origin.parent.parent.name == "src"
        and root.parent.name == "plugins"
        and (root.parent.parent / "vllm").is_dir()
    ):
        raise RuntimeError("Legacy bundled TT plugin detected. Migrate to standalone tenstorrent/vllm-tt-plugin.")


def _find_plugin_root() -> Path:
    """Locate the source checkout that matches the imported plugin."""
    spec = importlib.util.find_spec("vllm_tt_plugin")
    if spec is None or spec.origin is None:
        raise RuntimeError("Install the standalone tenstorrent/vllm-tt-plugin editable in the server environment.")
    origin = Path(spec.origin).resolve()
    # Editable source layout: <plugin_root>/src/vllm_tt_plugin/__init__.py
    installed_root = origin.parent.parent.parent
    configured = os.environ.get("VLLM_TT_PLUGIN_ROOT")
    root = Path(configured).expanduser().resolve() if configured else installed_root
    if origin != root / "src" / "vllm_tt_plugin" / "__init__.py":
        raise RuntimeError(
            f"VLLM_TT_PLUGIN_ROOT/source checkout does not match installed plugin {origin}. "
            "Install `tenstorrent/vllm-tt-plugin` editable with the server interpreter."
        )
    _reject_legacy_plugin_source(origin)
    return root


def _reject_legacy_serving_sources() -> None:
    """Reject known legacy vLLM and bundled-plugin sources."""
    origins: dict[str, Path] = {}
    for package in ("vllm", "vllm_tt_plugin"):
        spec = importlib.util.find_spec(package)
        if spec is None or spec.origin is None:
            raise RuntimeError(f"Install {package} with the server interpreter before serving.")
        origins[package] = Path(spec.origin).resolve()

    root = origins["vllm"].parent.parent
    # A wheel's parent is shared site-packages, not a vLLM source tree.
    is_source_tree = (root / "pyproject.toml").is_file() or (root / "setup.py").is_file()
    if is_source_tree and (
        (root / "plugins" / "vllm-tt-plugin").is_dir() or (root / "tests" / "tt").is_dir()
    ):
        raise RuntimeError(f"Legacy tenstorrent/vllm checkout detected at {root}. Install upstream vLLM.")

    _reject_legacy_plugin_source(origins["vllm_tt_plugin"])


def _find_plugin_tests_dir() -> Path:
    """Use tests belonging to the installed standalone plugin, never the fork."""
    root = _find_plugin_root()
    tests_dir = root / "tests" / "tt"
    if not (tests_dir / "conftest.py").is_file():
        raise RuntimeError(f"Canonical standalone plugin tests missing at {tests_dir}; inspect the pinned plugin layout.")
    return tests_dir


def _check_port_available(port: int) -> None:
    """Fail fast if `port` is already in use — vLLM will hang otherwise."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as e:
        raise RuntimeError(f"Port {port} is already in use ({e}). Pass --port to pick a different one.") from e
    finally:
        sock.close()


def _server_command(
    *,
    hf_model: str,
    max_num_seqs: int,
    block_size: int,
    port: int,
    max_model_len: Optional[int],
    tt_config: dict[str, Any],
    additional_args: List[str],
    enable_prefix_caching: Optional[bool] = None,
) -> List[str]:
    """Build the server argv. `enable_prefix_caching=None` leaves the vLLM default."""
    cmd: List[str] = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        hf_model,
        "--block_size",
        str(block_size),
        "--max_num_seqs",
        str(max_num_seqs),
        "--port",
        str(port),
    ]
    if max_model_len is not None:
        cmd += ["--max_model_len", str(max_model_len)]
    if enable_prefix_caching is True:
        cmd.append("--enable-prefix-caching")
    elif enable_prefix_caching is False:
        cmd.append("--no-enable-prefix-caching")
    # Pass TT plugin config as a single JSON dict so JSON quoting can't be
    # mangled by intermediate shells. The dict already has
    # `sample_on_device_mode` enforced; callers extend via `tt_config`.
    cmd += ["--additional-config", json.dumps({"tt": tt_config})]
    cmd += additional_args
    return cmd


def _launch_server(
    *,
    hf_model: str,
    mesh_device: str,
    max_num_seqs: int,
    block_size: int,
    port: int,
    log_file: Path,
    max_model_len: Optional[int],
    tt_config: dict[str, Any],
    additional_args: List[str],
    enable_prefix_caching: Optional[bool] = None,
) -> subprocess.Popen:
    """
    Launch vLLM via `python -m vllm.entrypoints.openai.api_server`.

    Mirrors `vllm-tt-plugin/examples/server_example_tt.py` (which is what the
    nightly CI runs) but inlined — the example is just argv-munging + runpy.
    """
    cmd = _server_command(
        hf_model=hf_model,
        max_num_seqs=max_num_seqs,
        block_size=block_size,
        port=port,
        max_model_len=max_model_len,
        tt_config=tt_config,
        additional_args=additional_args,
        enable_prefix_caching=enable_prefix_caching,
    )

    env = {
        **os.environ,
        "MESH_DEVICE": mesh_device,
        "HF_MODEL": hf_model,
        "VLLM_RPC_TIMEOUT": str(DEFAULT_VLLM_RPC_TIMEOUT_MS),
    }

    print(f"Launching vLLM server for {hf_model} on {mesh_device}")
    print(f"  cmd: {' '.join(shlex.quote(c) for c in cmd)}")
    print(f"  log: {log_file}")

    log_handle = open(log_file, "wb")
    return subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT, env=env)


def _scan_log_for_fatal(log_file: Path) -> Optional[str]:
    if not log_file.exists():
        return None
    try:
        text = log_file.read_text(errors="replace")
    except OSError:
        return None
    for pattern in _FATAL_LOG_PATTERNS:
        if pattern in text:
            return pattern
    return None


def _wait_for_server(
    *,
    proc: subprocess.Popen,
    port: int,
    log_file: Path,
    timeout_seconds: int,
) -> None:
    """Poll /health, fast-failing on launcher exit or fatal-marker in the log."""
    print(f"Waiting up to {timeout_seconds}s for server to become ready...")
    url = f"http://localhost:{port}/health"
    interval = 10
    elapsed = 0

    while elapsed < timeout_seconds:
        try:
            if requests.get(url, timeout=2).status_code == 200:
                print(f"  Server ready after ~{elapsed}s")
                return
        except requests.exceptions.RequestException:
            pass

        if proc.poll() is not None:
            raise RuntimeError(
                f"Server launcher exited with code {proc.returncode} before becoming ready. Inspect {log_file}."
            )

        fatal = _scan_log_for_fatal(log_file)
        if fatal:
            raise RuntimeError(f"Fatal marker {fatal!r} found in {log_file}. Server cannot start.")

        time.sleep(interval)
        elapsed += interval

    raise RuntimeError(f"Server did not become ready in {timeout_seconds}s. Inspect {log_file}.")


def _probe_external_server(server_url: str) -> None:
    """Verify an externally-managed server is reachable before running checks."""
    health = f"{server_url.rstrip('/')}/health"
    try:
        resp = requests.get(health, timeout=5)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"Server not reachable at {health} ({e}). Start it first with `--stages serve` " "or check --server-url."
        ) from e
    if resp.status_code != 200:
        raise RuntimeError(f"Server at {health} returned {resp.status_code}; expected 200.")


def _run_plugin_sampling_tests(
    *,
    server_url: str,
    hf_model: str,
    log_file: Path,
    server_log: Path,
    max_num_seqs: int,
    sampling_profile: str,
) -> bool:
    """
    Invoke `vllm-tt-plugin/tests/tt/` against the live server. This is the same
    test suite the nightly CI runs (greedy determinism, seeded reproducibility,
    seed variety, logprobs, penalties, isolation, ...). Reusing it instead of
    re-implementing ad-hoc copies means our checks track the canonical ones.

    On failure, scan the server log for engine-core fatal markers and surface
    them — a mid-run crash typically shows up as cryptic empty-response errors
    in the pytest output, with the real cause buried in the server log.
    """
    tests_dir = _find_plugin_tests_dir()
    if sampling_profile == SAMPLING_PROFILE_FULL:
        test_targets: List[str] = [str(tests_dir)]
    elif sampling_profile == SAMPLING_PROFILE_SMOKE:
        test_targets = []
        for rel_nodeid in _SMOKE_SAMPLING_TESTS:
            rel_path, _, selector = rel_nodeid.partition("::")
            nodeid = str(tests_dir / rel_path)
            if selector:
                nodeid += f"::{selector}"
            test_targets.append(nodeid)
    else:
        raise RuntimeError(
            f"Unsupported sampling profile {sampling_profile!r}. "
            f"Expected one of: {SAMPLING_PROFILE_FULL}, {SAMPLING_PROFILE_SMOKE}."
        )

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *test_targets,
        "-v",
        f"--tt-server-url={server_url}",
        f"--tt-model-name={hf_model}",
        f"--tt-max-num-seqs={max_num_seqs}",
    ]
    print("\n=== Running plugin sampling tests ===")
    print(f"  profile: {sampling_profile} ({len(test_targets)} target(s))")
    print(f"  tt-max-num-seqs: {max_num_seqs}")
    print(f"  cmd: {' '.join(shlex.quote(c) for c in cmd)}")
    print(f"  log: {log_file}")

    with open(log_file, "wb") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)

    if result.returncode == 0:
        print("  PASS")
        return True

    print(f"  FAIL (pytest exit {result.returncode}) — inspect {log_file}")
    fatal = _scan_log_for_fatal(server_log)
    if fatal:
        print(
            f"  Note: server log contains fatal marker {fatal!r} — the engine "
            "crashed mid-run. The pytest 'NoneType' / empty-response errors are "
            f"a symptom; check {server_log} around that marker for the real cause."
        )
    return False


def _run_qualitative_prompts(
    *,
    server_url: str,
    hf_model: str,
    prompts_file: Path,
    output_dir: Path,
) -> None:
    """Run prompts through the server and save completions for manual review."""
    print(f"\n=== Running qualitative prompts from {prompts_file} ===")

    if not prompts_file.exists():
        raise RuntimeError(f"Prompts file not found: {prompts_file}")

    prompts = [p.strip() for p in prompts_file.read_text().split("\n\n") if p.strip()]
    if not prompts:
        raise RuntimeError(f"No prompts found in {prompts_file}")

    print(f"  Loaded {len(prompts)} prompts")
    client = openai.OpenAI(base_url=f"{server_url.rstrip('/')}/v1", api_key="dummy")
    prompt_mode = _qualitative_prompt_mode(hf_model)
    print(f"  Prompt mode: {prompt_mode}")

    results: List[dict[str, Any]] = []
    for i, prompt in enumerate(prompts, 1):
        print(f"\n  Prompt {i}/{len(prompts)}: {prompt[:60]}...")

        greedy_text = _request_qualitative_completion(
            client=client,
            hf_model=hf_model,
            prompt=prompt,
            prompt_mode=prompt_mode,
            temperature=0.0,
        )
        sampled_text = _request_qualitative_completion(
            client=client,
            hf_model=hf_model,
            prompt=prompt,
            prompt_mode=prompt_mode,
            temperature=0.7,
            top_p=0.9,
        )

        results.append(
            {
                "prompt": prompt,
                "prompt_mode": prompt_mode,
                "greedy_completion": greedy_text,
                "sampled_completion": sampled_text,
            }
        )
        print(f"    Greedy : {greedy_text[:80].replace(chr(10), ' ')!r}")
        print(f"    Sampled: {sampled_text[:80].replace(chr(10), ' ')!r}")

    output_file = output_dir / "vllm_qualitative_outputs.json"
    output_file.write_text(json.dumps(results, indent=2))
    print(f"\nSaved {len(results)} completions to {output_file}")
    print("Read both completions for each prompt and judge:")
    print("  - coherent and on-topic")
    print("  - no repetition loops or gibberish")
    print("  - greedy and sampled outputs both reasonable")


def _qualitative_prompt_mode(hf_model: str) -> str:
    """Use chat endpoints exactly when the checkpoint declares a chat template."""
    tokenizer = AutoTokenizer.from_pretrained(hf_model)
    return "chat" if bool(getattr(tokenizer, "chat_template", None)) else "completion"


def _request_qualitative_completion(
    *,
    client: openai.OpenAI,
    hf_model: str,
    prompt: str,
    prompt_mode: str,
    temperature: float,
    top_p: Optional[float] = None,
) -> str:
    request_args: dict[str, Any] = {
        "model": hf_model,
        "max_tokens": 256,
        "temperature": temperature,
    }
    if top_p is not None:
        request_args["top_p"] = top_p

    if prompt_mode == "chat":
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            **request_args,
        )
        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("Chat completion returned no text content")
        return content
    if prompt_mode == "completion":
        return client.completions.create(prompt=prompt, **request_args).choices[0].text
    raise ValueError(f"Unsupported qualitative prompt mode: {prompt_mode!r}")


def _vllm_cli_command() -> List[str]:
    """Return a vLLM CLI invocation in the active Python environment."""
    vllm_exe = shutil.which("vllm")
    if vllm_exe is not None:
        return [vllm_exe]
    return [sys.executable, "-m", "vllm.entrypoints.cli.main"]


def _quoted_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def _json_number(data: dict[str, Any], key: str) -> Optional[float]:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _decode_tps_from_ms(ms: Optional[float]) -> Optional[float]:
    return (1000.0 / ms) if ms and ms > 0 else None


def _metric_summary(raw: dict[str, Any], metric: str) -> dict[str, Optional[float]]:
    return {
        "p50": _json_number(raw, f"median_{metric}_ms"),
        "p99": _json_number(raw, f"p99_{metric}_ms"),
        "mean": _json_number(raw, f"mean_{metric}_ms"),
    }


def _write_normalized_vllm_benchmark_summary(
    *,
    raw: dict[str, Any],
    output_file: Path,
    raw_result_file: Path,
    command: List[str],
    profile_name: str,
    comparison_scope: str,
    prompt_len: int,
    output_len: int,
    num_requests: int,
    concurrency: Optional[int],
    temperature: Optional[float],
    dataset_name: str = "random",
) -> dict[str, Any]:
    total_output_tokens = _json_number(raw, "total_output_tokens")
    requested_output_tokens = output_len * num_requests
    mean_tpot_ms = _json_number(raw, "mean_tpot_ms")
    median_itl_ms = _json_number(raw, "median_itl_ms")
    mean_itl_ms = _json_number(raw, "mean_itl_ms")

    summary: dict[str, Any] = {
        "source": "vllm bench serve",
        "profile": profile_name,
        "comparison_scope": comparison_scope,
        "raw_result_file": str(raw_result_file),
        "command": command,
        "command_string": _quoted_cmd(command),
        "config": {
            "prompt_len": prompt_len,
            "output_len": output_len,
            "num_requests": num_requests,
            "concurrency": concurrency,
            "temperature": temperature,
            "generation_config_mode": "server_default" if temperature is None else "explicit",
            "dataset_name": dataset_name,
            "ignore_eos": True,
        },
        "elapsed_s": _json_number(raw, "duration"),
        "completed_requests": raw.get("completed"),
        "total_input_tokens": _json_number(raw, "total_input_tokens"),
        "total_output_tokens": total_output_tokens,
        "requested_output_tokens": requested_output_tokens,
        "missing_output_tokens": (
            max(0, requested_output_tokens - int(total_output_tokens)) if total_output_tokens is not None else None
        ),
        "ttft_ms": _metric_summary(raw, "ttft"),
        "tpot_ms": _metric_summary(raw, "tpot"),
        "itl_ms": _metric_summary(raw, "itl"),
        "e2el_ms": _metric_summary(raw, "e2el"),
        "itl_p50_decode_tps": _decode_tps_from_ms(median_itl_ms),
        "itl_mean_decode_tps": _decode_tps_from_ms(mean_itl_ms),
        "vllm_mean_tpot_decode_tps": _decode_tps_from_ms(mean_tpot_ms),
        "mean_per_request_decode_tps": _decode_tps_from_ms(mean_tpot_ms),
        "mean_per_request_decode_tps_basis": (
            "1000 / mean_tpot_ms from vllm bench serve; this is the TPOT-derived "
            "decode value. For burst-serving profiles, scheduler admission and "
            "chunked prefill can affect TPOT, so use the single-user profile for "
            "headline decode t/s/u."
        ),
        "output_throughput_tok_per_s": _json_number(raw, "output_throughput"),
        "request_throughput_per_s": _json_number(raw, "request_throughput"),
        "total_token_throughput_tok_per_s": _json_number(raw, "total_token_throughput"),
    }
    output_file.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def _print_file_tail(log_file: Path, lines: int = 40) -> None:
    if not log_file.exists():
        return
    print(f"\n=== Log tail ({log_file}, last {lines} lines) ===")
    for line in log_file.read_text(errors="replace").splitlines()[-lines:]:
        print(line)


def _run_serving_benchmark(
    *,
    server_url: str,
    hf_model: str,
    output_dir: Path,
    profile_name: str,
    comparison_scope: str,
    raw_result_filename: str,
    summary_filename: str,
    log_filename: str,
    prompt_len: int,
    output_len: int,
    num_requests: int,
    concurrency: Optional[int],
    temperature: Optional[float],
    additional_args: List[str],
    dataset_args: Optional[List[str]] = None,
    dataset_name: str = "random",
) -> dict[str, Any]:
    """
    Run one vLLM serving benchmark profile.

    The primary caller writes `vllm_result.json` and `vllm_benchmark.json`;
    secondary profiles use distinct filenames. `dataset_args` replaces the
    default random-dataset arguments when a profile needs another dataset.
    """
    print(f"\n=== Running vLLM serving benchmark: {profile_name} ===")
    print(
        f"  prompt_len={prompt_len}, output_len={output_len}, "
        f"num_requests={num_requests}, concurrency={concurrency}, temperature={temperature}"
    )
    print(f"  comparison_scope: {comparison_scope}")

    raw_result_file = output_dir / raw_result_filename
    summary_file = output_dir / summary_filename
    log_file = output_dir / log_filename
    for stale_file in (raw_result_file, summary_file):
        if stale_file.exists():
            stale_file.unlink()

    cmd: List[str] = [
        *_vllm_cli_command(),
        "bench",
        "serve",
        "--backend",
        "vllm",
        "--model",
        hf_model,
        "--base-url",
        server_url.rstrip("/"),
        "--endpoint",
        "/v1/completions",
        *(
            dataset_args
            if dataset_args is not None
            else ["--dataset-name", "random", "--random-input-len", str(prompt_len), "--random-output-len", str(output_len)]
        ),
        "--num-prompts",
        str(num_requests),
        "--ignore-eos",
        "--percentile-metrics",
        "ttft,tpot,itl,e2el",
        "--save-result",
        "--result-filename",
        str(raw_result_file),
    ]
    if concurrency is not None:
        cmd.extend(["--max-concurrency", str(concurrency)])
    if temperature is not None:
        cmd.extend(["--temperature", str(temperature)])
    cmd.extend(additional_args)

    print(f"  command: {_quoted_cmd(cmd)}")
    print(f"  log: {log_file}")
    with open(log_file, "wb") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        _print_file_tail(log_file)
        raise RuntimeError(f"`vllm bench serve` failed with exit code {result.returncode}; inspect {log_file}")
    if not raw_result_file.exists():
        _print_file_tail(log_file)
        raise RuntimeError(f"`vllm bench serve` succeeded but did not write {raw_result_file}")

    raw = json.loads(raw_result_file.read_text())
    expected_completed = raw.get("num_prompts", num_requests)
    if raw.get("completed") != expected_completed:
        _print_file_tail(log_file)
        raise RuntimeError(
            "`vllm bench serve` did not complete every prompt: "
            f"completed={raw.get('completed')} num_prompts={expected_completed}. "
            f"Raw result: {raw_result_file}"
        )

    summary = _write_normalized_vllm_benchmark_summary(
        raw=raw,
        output_file=summary_file,
        raw_result_file=raw_result_file,
        command=cmd,
        profile_name=profile_name,
        comparison_scope=comparison_scope,
        prompt_len=prompt_len,
        output_len=output_len,
        num_requests=num_requests,
        concurrency=concurrency,
        temperature=temperature,
        dataset_name=dataset_name,
    )

    def _fmt(v: Optional[float], unit: str) -> str:
        return f"{v:.1f}{unit}" if v is not None else "n/a"

    print(f"\n=== Serving benchmark summary: {profile_name} ===")
    print(f"  Requests : {summary['completed_requests']}/{expected_completed} completed")
    print(f"  TTFT     : P50={_fmt(summary['ttft_ms']['p50'], 'ms')}  P99={_fmt(summary['ttft_ms']['p99'], 'ms')}")
    print(f"  TPOT     : mean={_fmt(summary['tpot_ms']['mean'], 'ms')}  P99={_fmt(summary['tpot_ms']['p99'], 'ms')}")
    print(f"  ITL      : P50={_fmt(summary['itl_ms']['p50'], 'ms')}  P99={_fmt(summary['itl_ms']['p99'], 'ms')}")
    print(f"  Output   : {_fmt(summary['output_throughput_tok_per_s'], ' tok/s')} aggregate")
    print(f"  Decode   : {_fmt(summary['vllm_mean_tpot_decode_tps'], ' t/s/u')} from mean TPOT")
    print(f"  Raw      : {raw_result_file}")
    print(f"  Summary  : {summary_file}")
    return summary


def _parse_prometheus_counters(text: str) -> dict[str, float]:
    """Sum every sample of each metric; `_total` is folded into the base name."""
    totals: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name_part, _, value_part = line.rpartition(" ")
        if not name_part or not value_part:
            continue
        name = name_part.split("{", 1)[0]
        if name.endswith("_total"):
            name = name[: -len("_total")]
        try:
            value = float(value_part)
        except ValueError:
            continue
        totals[name] = totals.get(name, 0.0) + value
    return totals


def _fetch_prefix_cache_counters(server_url: str) -> dict[str, float]:
    resp = requests.get(f"{server_url.rstrip('/')}/metrics", timeout=30)
    resp.raise_for_status()
    counters = _parse_prometheus_counters(resp.text)
    return {name: counters.get(name, 0.0) for name in PREFIX_CACHE_COUNTERS}


def _counter_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, Any]:
    queries = after["vllm:prefix_cache_queries"] - before["vllm:prefix_cache_queries"]
    hits = after["vllm:prefix_cache_hits"] - before["vllm:prefix_cache_hits"]
    return {
        "prefix_cache_queries_delta": queries,
        "prefix_cache_hits_delta": hits,
        "hit_rate": (hits / queries) if queries > 0 else None,
    }


def _reset_prefix_cache(server_url: str) -> bool:
    try:
        resp = requests.post(f"{server_url.rstrip('/')}/reset_prefix_cache", timeout=60)
    except requests.exceptions.RequestException:
        return False
    return resp.status_code == 200


def _natural_text(tokenizer: Any, pieces: List[str], target_tokens: int, *, reverse: bool = False) -> str:
    order = list(reversed(pieces)) if reverse else list(pieces)
    text = ""
    index = 0
    while len(tokenizer.encode(text)) < target_tokens:
        text = (text + "\n\n" if text else "") + order[index % len(order)]
        index += 1
    token_ids = tokenizer.encode(text)[:target_tokens]
    return tokenizer.decode(token_ids, skip_special_tokens=True)


def _build_prefix_cache_prompt(tokenizer: Any, prompts_file: Path, target_tokens: int) -> tuple[str, int]:
    """Deterministic natural text of about `target_tokens` tokens."""
    pieces = [p.strip() for p in prompts_file.read_text().split("\n\n") if p.strip()]
    if not pieces:
        raise RuntimeError(f"No prompts found in {prompts_file}")
    prompt = _natural_text(tokenizer, pieces, target_tokens)
    return prompt, len(tokenizer.encode(prompt))


def _build_resume_suffix(tokenizer: Any, prompts_file: Path, suffix_tokens: int) -> str:
    """Different natural text appended to the shared prefix so the resume is a long continuation."""
    pieces = [p.strip() for p in prompts_file.read_text().split("\n\n") if p.strip()]
    return "\n\n" + _natural_text(tokenizer, pieces, suffix_tokens, reverse=True)


def _stream_completion(*, server_url: str, hf_model: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    """Greedy streamed completion with TTFT measured at the first content chunk."""
    payload = {
        "model": hf_model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "logprobs": 1,
    }
    started = time.perf_counter()
    ttft_ms: Optional[float] = None
    text_parts: List[str] = []
    tokens: List[str] = []
    token_logprobs: List[Optional[float]] = []
    with requests.post(f"{server_url.rstrip('/')}/v1/completions", json=payload, stream=True, timeout=1800) as resp:
        resp.raise_for_status()
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            piece = choice.get("text") or ""
            logprobs = choice.get("logprobs") or {}
            chunk_tokens = logprobs.get("tokens") or []
            chunk_logprobs = logprobs.get("token_logprobs") or []
            if ttft_ms is None and (piece or chunk_tokens):
                ttft_ms = (time.perf_counter() - started) * 1000.0
            text_parts.append(piece)
            tokens.extend(str(t) for t in chunk_tokens)
            token_logprobs.extend(chunk_logprobs)
    return {
        "ttft_ms": ttft_ms,
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "text": "".join(text_parts),
        "tokens": tokens,
        "token_logprobs": token_logprobs,
    }


def _common_prefix_len(a: List[Any], b: List[Any]) -> int:
    count = 0
    for x, y in zip(a, b):
        if x != y:
            break
        count += 1
    return count


def _prefix_cache_verdict(record: dict[str, Any], thresholds: dict[str, float]) -> tuple[str, List[str]]:
    reasons: List[str] = []
    requests_log = record.get("requests") or []
    cold = [r for r in requests_log if r["kind"] == "cold"]
    warm = [r for r in requests_log if r["kind"] == "warm"]
    if record.get("apc_log_enabled") is False:
        reasons.append("server log reports automatic prefix caching disabled")
    if not cold or not warm:
        reasons.append("the check needs at least one cold and one warm request")
        return "fail", reasons
    for r in cold:
        if r["hit_rate"] is not None and r["hit_rate"] > thresholds["cold_hit_rate_max"]:
            reasons.append(f"{r['label']}: cold request hit rate {r['hit_rate']:.2f} > {thresholds['cold_hit_rate_max']}")
    for r in warm:
        if r["hit_rate"] is None or r["hit_rate"] < thresholds["warm_hit_rate_min"]:
            reasons.append(f"{r['label']}: warm request hit rate {r['hit_rate']} < {thresholds['warm_hit_rate_min']}")
    base = cold[0]
    warm_ttfts = [r["ttft_ms"] for r in warm if r["ttft_ms"] is not None]
    if base["ttft_ms"] is None or not warm_ttfts:
        reasons.append("TTFT was not measured for the cold or warm requests")
    else:
        ratio = sorted(warm_ttfts)[len(warm_ttfts) // 2] / base["ttft_ms"]
        record["ttft_ratio"] = ratio
        if ratio > thresholds["ttft_ratio_max"]:
            reasons.append(f"warm/cold TTFT ratio {ratio:.3f} > {thresholds['ttft_ratio_max']}")
    for r in warm:
        if not base["tokens"] or not r["tokens"]:
            reasons.append(f"{r['label']}: no tokens returned")
            continue
        if base["tokens"][0] != r["tokens"][0]:
            reasons.append(f"{r['label']}: first token differs from cold ({base['tokens'][0]!r} vs {r['tokens'][0]!r})")
        base_lp = base["token_logprobs"][0] if base["token_logprobs"] else None
        warm_lp = r["token_logprobs"][0] if r["token_logprobs"] else None
        if base_lp is None or warm_lp is None:
            reasons.append(f"{r['label']}: first-token logprob missing")
        elif abs(base_lp - warm_lp) > thresholds["first_token_logprob_delta_max"]:
            reasons.append(
                f"{r['label']}: first-token logprob delta {abs(base_lp - warm_lp):.3f} > "
                f"{thresholds['first_token_logprob_delta_max']}"
            )
        identical = _common_prefix_len(base["tokens"], r["tokens"])
        r["greedy_identical_through"] = identical
        if identical < min(len(base["tokens"]), int(thresholds["greedy_identical_min_tokens"])):
            reasons.append(
                f"{r['label']}: greedy output diverges after {identical} tokens "
                f"(< {int(thresholds['greedy_identical_min_tokens'])})"
            )
    shared = record.get("shared_prefix_bench")
    if isinstance(shared, dict) and shared.get("hit_rate") is not None:
        if shared["hit_rate"] < thresholds["shared_prefix_hit_rate_min"]:
            reasons.append(
                f"shared-prefix benchmark hit rate {shared['hit_rate']:.2f} < {thresholds['shared_prefix_hit_rate_min']}"
            )
    resume_warm = [r for r in requests_log if r["kind"] == "resume_warm"]
    resume_cold = [r for r in requests_log if r["kind"] == "resume_cold"]
    if resume_warm and resume_cold:
        warm_r, cold_r = resume_warm[0], resume_cold[0]
        if warm_r["hit_rate"] is None or warm_r["hit_rate"] <= 0.5:
            reasons.append(f"resume_warm: shared prefix was not served from the cache (hit rate {warm_r['hit_rate']})")
        if cold_r["hit_rate"] is not None and cold_r["hit_rate"] > thresholds["cold_hit_rate_max"]:
            reasons.append(f"resume_cold: reference still hit the cache (hit rate {cold_r['hit_rate']:.2f})")
        if not warm_r["tokens"] or not cold_r["tokens"]:
            reasons.append("long resume: no tokens returned")
        else:
            if warm_r["tokens"][0] != cold_r["tokens"][0]:
                reasons.append("long resume: first token differs between the resumed and the cold request")
            warm_lp = warm_r["token_logprobs"][0] if warm_r["token_logprobs"] else None
            cold_lp = cold_r["token_logprobs"][0] if cold_r["token_logprobs"] else None
            if warm_lp is None or cold_lp is None:
                reasons.append("long resume: first-token logprob missing")
            elif abs(warm_lp - cold_lp) > thresholds["first_token_logprob_delta_max"]:
                reasons.append(
                    f"long resume: first-token logprob delta {abs(warm_lp - cold_lp):.3f} > "
                    f"{thresholds['first_token_logprob_delta_max']}"
                )
            identical = _common_prefix_len(warm_r["tokens"], cold_r["tokens"])
            warm_r["greedy_identical_through"] = identical
            if identical < min(len(cold_r["tokens"]), int(thresholds["greedy_identical_min_tokens"])):
                reasons.append(
                    f"long resume: greedy output diverges after {identical} tokens "
                    f"(< {int(thresholds['greedy_identical_min_tokens'])})"
                )
    return ("pass" if not reasons else "fail"), reasons


def _server_log_apc_state(server_log: Optional[Path]) -> Optional[bool]:
    if server_log is None or not server_log.exists():
        return None
    text = server_log.read_text(errors="replace")
    if APC_ENABLED_LOG_LINE in text:
        return True
    if APC_DISABLED_LOG_LINE in text:
        return False
    return None


def _run_prefix_cache_check(
    *,
    server_url: str,
    hf_model: str,
    output_dir: Path,
    prompts_file: Path,
    prompt_tokens: int,
    output_tokens: int,
    thresholds: dict[str, float],
    server_log: Optional[Path],
    shared_prefix_bench: bool,
    additional_benchmark_args: List[str],
    resume_suffix_tokens: int = DEFAULT_PREFIX_CACHE_RESUME_SUFFIX_TOKENS,
) -> dict[str, Any]:
    print("\n=== Running prefix-cache check ===")
    tokenizer = AutoTokenizer.from_pretrained(hf_model)
    prompt, measured_tokens = _build_prefix_cache_prompt(tokenizer, prompts_file, prompt_tokens)
    print(f"  prompt tokens: {measured_tokens} (target {prompt_tokens}), output tokens: {output_tokens}")

    requests_log: List[dict[str, Any]] = []

    def one(label: str, kind: str, text: str = prompt) -> None:
        before = _fetch_prefix_cache_counters(server_url)
        out = _stream_completion(server_url=server_url, hf_model=hf_model, prompt=text, max_tokens=output_tokens)
        after = _fetch_prefix_cache_counters(server_url)
        entry = {
            "label": label,
            "kind": kind,
            "ttft_ms": out["ttft_ms"],
            "elapsed_ms": out["elapsed_ms"],
            **_counter_delta(before, after),
            "tokens": out["tokens"],
            "token_logprobs": out["token_logprobs"],
            "text": out["text"],
        }
        requests_log.append(entry)
        ttft = f"{entry['ttft_ms']:.1f} ms" if entry["ttft_ms"] is not None else "n/a"
        rate = f"{entry['hit_rate']:.2f}" if entry["hit_rate"] is not None else "n/a"
        print(f"  {label:<6} {kind:<4} ttft={ttft:>12} hit_rate={rate} tokens={len(entry['tokens'])}")

    reset_before = _reset_prefix_cache(server_url)
    print(f"  reset_prefix_cache before cold1: {'ok' if reset_before else 'unavailable'}")
    one("cold1", "cold")
    one("warm1", "warm")
    one("warm2", "warm")
    reset_ok = _reset_prefix_cache(server_url)
    print(f"  reset_prefix_cache: {'ok' if reset_ok else 'unavailable'}")
    long_resume: Optional[dict[str, Any]] = None
    if reset_ok:
        one("cold2", "cold")
        one("warm3", "warm")
        suffix = _build_resume_suffix(tokenizer, prompts_file, resume_suffix_tokens)
        extended = prompt + suffix
        one("resume_warm", "resume_warm", extended)
        if _reset_prefix_cache(server_url):
            one("resume_cold", "resume_cold", extended)
            long_resume = {
                "continuation_tokens": len(tokenizer.encode(extended)) - measured_tokens,
                "checked": True,
            }
        else:
            long_resume = {"checked": False, "reason": "reset_prefix_cache failed before the cold reference"}
    else:
        long_resume = {
            "checked": False,
            "reason": "reset_prefix_cache unavailable; set VLLM_SERVER_DEV_MODE=1 on the server to enable it",
        }

    shared: Optional[dict[str, Any]] = None
    if shared_prefix_bench:
        before = _fetch_prefix_cache_counters(server_url)
        summary = _run_serving_benchmark(
            server_url=server_url,
            hf_model=hf_model,
            output_dir=output_dir,
            profile_name="shared_prefix",
            comparison_scope=(
                "Shared-prefix workload (4 prefixes of 2048 tokens, 128-token unique suffixes) for prefix-cache "
                "hit rate and completion. Not a headline latency number."
            ),
            raw_result_filename="vllm_shared_prefix_result.json",
            summary_filename="vllm_shared_prefix_benchmark.json",
            log_filename="vllm_shared_prefix_benchmark.log",
            prompt_len=2048 + 128,
            output_len=32,
            num_requests=32,
            concurrency=8,
            temperature=0.0,
            additional_args=additional_benchmark_args,
            dataset_args=list(SHARED_PREFIX_BENCH_ARGS),
            dataset_name="prefix_repetition",
        )
        after = _fetch_prefix_cache_counters(server_url)
        shared = {
            **_counter_delta(before, after),
            "completed_requests": summary["completed_requests"],
            "summary_file": str(output_dir / "vllm_shared_prefix_benchmark.json"),
        }
        shared["hit_rate"] = shared.pop("hit_rate")

    record: dict[str, Any] = {
        "prompt_tokens": measured_tokens,
        "output_tokens": output_tokens,
        "thresholds": thresholds,
        "apc_log_enabled": _server_log_apc_state(server_log),
        "reset_prefix_cache_before_cold1": reset_before,
        "reset_prefix_cache_available": reset_ok,
        "long_resume": long_resume,
        "requests": requests_log,
        "shared_prefix_bench": shared,
    }
    record["status"], record["reasons"] = _prefix_cache_verdict(record, thresholds)
    output_file = output_dir / "prefix_cache_check.json"
    output_file.write_text(json.dumps(record, indent=2) + "\n")
    print(f"  status: {record['status']} -> {output_file}")
    for reason in record["reasons"]:
        print(f"    {reason}")
    return record


BURST_CONFIG_KEYS = ("prompt_len", "output_len", "num_requests", "concurrency")


def _attach_serial_reference(
    summary_file: Path,
    summary: dict[str, Any],
    serial: Optional[dict[str, Any]],
    serial_path: Optional[Path],
) -> dict[str, Any]:
    """Record burst/serial TTFT ratios. `serial` is read before the burst run so the same path is safe."""
    if serial is None:
        return summary
    serial_ttft = serial.get("ttft_ms") or {}
    burst_ttft = summary.get("ttft_ms") or {}
    serial_config = serial.get("config") or {}
    burst_config = summary.get("config") or {}
    config_matches = all(serial_config.get(key) == burst_config.get(key) for key in BURST_CONFIG_KEYS)

    def ratio(key: str) -> Optional[float]:
        s = serial_ttft.get(key)
        b = burst_ttft.get(key)
        return (b / s) if isinstance(s, (int, float)) and s > 0 and isinstance(b, (int, float)) else None

    summary["serial_reference"] = {
        "path": str(serial_path) if serial_path else None,
        "serial_config": {key: serial_config.get(key) for key in BURST_CONFIG_KEYS},
        "config_matches": config_matches,
        "serial_ttft_ms": serial_ttft,
        "ttft_ratio_p99": ratio("p99"),
        "ttft_ratio_mean": ratio("mean"),
        "ttft_ratio_p50": ratio("p50"),
    }
    summary_file.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"  serial reference: p99 ratio={summary['serial_reference']['ttft_ratio_p99']}, "
        f"mean ratio={summary['serial_reference']['ttft_ratio_mean']}, config_matches={config_matches}"
    )
    return summary


def _write_run_summary(path: Path, summary: dict[str, Any]) -> None:
    """Merge into an existing summary so a hold-open server and attached check runs share one file."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
    merged = dict(existing)
    for key, value in summary.items():
        if key == "outputs":
            merged["outputs"] = {**(existing.get("outputs") or {}), **value}
        elif key == "stages":
            merged["stages"] = sorted(set(existing.get("stages") or []) | set(value))
        elif value is None and existing.get(key) is not None:
            continue
        else:
            merged[key] = value
    path.write_text(json.dumps(merged, indent=2) + "\n")


def _shutdown(proc: subprocess.Popen, log_file: Path) -> None:
    """Terminate the server; fall back to SIGKILL; show log tail."""
    if proc.poll() is not None:
        print(f"\nServer launcher already exited (code {proc.returncode}).")
    else:
        print(f"\nShutting down server (PID {proc.pid})...")
        proc.terminate()
        try:
            proc.wait(timeout=15)
            print("  Terminated cleanly.")
        except subprocess.TimeoutExpired:
            print("  Did not exit after SIGTERM; sending SIGKILL.")
            proc.kill()
            proc.wait()

    if log_file.exists():
        print(f"\n=== Server log tail ({log_file}, last 30 lines) ===")
        for line in log_file.read_text(errors="replace").splitlines()[-30:]:
            print(line)


def _hold_until_signal(proc: subprocess.Popen, log_file: Path) -> None:
    """Block until SIGINT/SIGTERM, or until the server exits on its own."""
    stop = False

    def _handler(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handler)

    print("Server is running. Press Ctrl-C (or send SIGTERM) to stop.")
    try:
        while not stop:
            if proc.poll() is not None:
                raise RuntimeError(f"Server exited unexpectedly with code {proc.returncode}. Inspect {log_file}.")
            # vLLM's engine can die while the launcher keeps running; the fatal
            # markers catch that case (same scan as _wait_for_server).
            fatal = _scan_log_for_fatal(log_file)
            if fatal:
                raise RuntimeError(f"Fatal marker {fatal!r} found in {log_file}. Server engine is dead.")
            time.sleep(2)
    except KeyboardInterrupt:
        pass


def _parse_stages(raw: str) -> List[str]:
    stages = [s.strip() for s in raw.split(",") if s.strip()]
    if not stages:
        raise argparse.ArgumentTypeError("--stages must list at least one stage")
    unknown = [s for s in stages if s not in ALL_STAGES]
    if unknown:
        raise argparse.ArgumentTypeError(f"Unknown stages {unknown}. Valid stages: {list(ALL_STAGES)}")
    deduped: List[str] = []
    for s in stages:
        if s not in deduped:
            deduped.append(s)
    return deduped


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the vLLM server readiness check.")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for this run's outputs. Default: <model_dir>/readiness_vllm, the root the stage gates read.",
    )
    parser.add_argument("--hf-model", type=str, required=True)
    parser.add_argument(
        "--stages",
        type=_parse_stages,
        default=list(DEFAULT_STAGES),
        help=("Comma-separated stages to run. Valid: " f"{','.join(ALL_STAGES)}. Default: {','.join(DEFAULT_STAGES)}."),
    )
    parser.add_argument(
        "--server-url",
        type=str,
        default=None,
        help=(
            "URL of an already-running vLLM server (e.g. http://localhost:8000). "
            "Required when `serve` is not in --stages; rejected when it is."
        ),
    )
    parser.add_argument(
        "--mesh-device",
        type=str,
        default=None,
        choices=sorted(MESH_SHAPES),
        help="Required when `serve` is in --stages; ignored otherwise.",
    )
    parser.add_argument(
        "--prompts",
        type=Path,
        default=Path(__file__).parent / "vllm_prompts.txt",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--max-num-seqs", type=int, default=DEFAULT_MAX_NUM_SEQS)
    parser.add_argument(
        "--sampling-profile",
        type=str,
        choices=(SAMPLING_PROFILE_FULL, SAMPLING_PROFILE_SMOKE),
        default=DEFAULT_SAMPLING_PROFILE,
        help=(
            "Sampling test selection profile. "
            f"`{SAMPLING_PROFILE_FULL}` runs the full plugin suite; "
            f"`{SAMPLING_PROFILE_SMOKE}` runs a small integration sanity subset."
        ),
    )
    parser.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)
    parser.add_argument(
        "--server-timeout",
        type=int,
        default=DEFAULT_SERVER_TIMEOUT_S,
        help=f"Seconds to wait for /health when launching (default {DEFAULT_SERVER_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--max-model-len",
        type=int,
        default=None,
        help="vLLM --max_model_len. Required for some models (e.g. Llama-3.1-8B on N150 caps at 32768).",
    )
    parser.add_argument(
        "--tt-config",
        type=str,
        default="{}",
        help=(
            "JSON dict merged into the TT plugin config under the `tt` namespace. "
            f"Always extends the runner's defaults ({DEFAULT_TT_CONFIG}). "
            'Example: \'{"trace_region_size": 85000000, "fabric_config": "FABRIC_1D"}\'.'
        ),
    )
    parser.add_argument(
        "--additional-server-args",
        type=str,
        default="",
        help=(
            "Catch-all for other vLLM CLI args not covered by the typed flags. "
            'Quoted, e.g. "--async-scheduling --tokenizer X". Avoid --plugin-config / '
            "--max_model_len here; use --tt-config / --max-model-len."
        ),
    )
    parser.add_argument(
        "--benchmark-prompt-len",
        type=int,
        default=DEFAULT_BENCH_PROMPT_LEN,
        help=(
            "Tokens per synthetic prompt for the primary single-user benchmark "
            f"(default {DEFAULT_BENCH_PROMPT_LEN})."
        ),
    )
    parser.add_argument(
        "--benchmark-output-len",
        type=int,
        default=DEFAULT_BENCH_OUTPUT_LEN,
        help=(
            "Tokens to generate per request in the primary single-user benchmark "
            f"(default {DEFAULT_BENCH_OUTPUT_LEN})."
        ),
    )
    parser.add_argument(
        "--benchmark-num-requests",
        type=int,
        default=DEFAULT_BENCH_NUM_REQUESTS,
        help=("Total requests sent in the primary single-user benchmark " f"(default {DEFAULT_BENCH_NUM_REQUESTS})."),
    )
    parser.add_argument(
        "--benchmark-concurrency",
        type=int,
        default=DEFAULT_BENCH_CONCURRENCY,
        help=(
            "`vllm bench serve --max-concurrency` for the primary single-user benchmark "
            f"(default {DEFAULT_BENCH_CONCURRENCY})."
        ),
    )
    parser.add_argument(
        "--benchmark-temperature",
        type=float,
        default=DEFAULT_BENCH_TEMPERATURE,
        help=(
            "Temperature passed to `vllm bench serve` for the benchmark. "
            f"Default {DEFAULT_BENCH_TEMPERATURE} preserves greedy t/s/u comparability. "
            "Use --benchmark-use-server-generation-config to omit this flag and reproduce exact nightly "
            "server-generation-config behavior."
        ),
    )
    parser.add_argument(
        "--benchmark-use-server-generation-config",
        action="store_true",
        help=(
            "Do not pass `--temperature` to `vllm bench serve`; the server/model generation_config decides "
            "sampling parameters. This matches current vLLM nightly behavior for models that do not add "
            "temperature via additional benchmark args, but is not comparable to the default greedy single-user "
            "t/s/u metric."
        ),
    )
    parser.add_argument(
        "--benchmark-ci-serving",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_CI_SERVING_BENCHMARK,
        help=(
            "Also run the vLLM-nightly-shaped 100/100/32 serving-burst profile as secondary evidence "
            f"(default {DEFAULT_CI_SERVING_BENCHMARK}). Use --no-benchmark-ci-serving to skip it."
        ),
    )
    parser.add_argument(
        "--ci-benchmark-prompt-len",
        type=int,
        default=DEFAULT_CI_BENCH_PROMPT_LEN,
        help=(
            "Tokens per synthetic prompt for the secondary CI serving-burst benchmark "
            f"(default {DEFAULT_CI_BENCH_PROMPT_LEN})."
        ),
    )
    parser.add_argument(
        "--ci-benchmark-output-len",
        type=int,
        default=DEFAULT_CI_BENCH_OUTPUT_LEN,
        help=(
            "Tokens to generate per request in the secondary CI serving-burst benchmark "
            f"(default {DEFAULT_CI_BENCH_OUTPUT_LEN})."
        ),
    )
    parser.add_argument(
        "--ci-benchmark-num-requests",
        type=int,
        default=DEFAULT_CI_BENCH_NUM_REQUESTS,
        help=(
            "Total requests sent in the secondary CI serving-burst benchmark "
            f"(default {DEFAULT_CI_BENCH_NUM_REQUESTS})."
        ),
    )
    parser.add_argument(
        "--ci-benchmark-concurrency",
        type=int,
        default=DEFAULT_CI_BENCH_CONCURRENCY,
        help=(
            "Optional `vllm bench serve --max-concurrency` for the secondary CI serving-burst benchmark. "
            "Default is unset, matching vLLM nightly CI."
        ),
    )
    parser.add_argument(
        "--additional-benchmark-args",
        type=str,
        default="",
        help=(
            "Catch-all for other `vllm bench serve` args not covered by the typed flags. "
            'Quoted, e.g. "--request-rate 0.25 --metric-percentiles 50,90,99".'
        ),
    )
    parser.add_argument(
        "--enable-prefix-caching",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Pass --enable-prefix-caching or --no-enable-prefix-caching to the launched server. "
            "Default leaves the vLLM default, which the TT plugin gates on model_capabilities."
        ),
    )
    parser.add_argument(
        "--server-log",
        type=Path,
        default=None,
        help="Server log to read the prefix-caching state from when attaching with --server-url.",
    )
    parser.add_argument(
        "--prefix-cache-prompt-tokens",
        type=int,
        default=DEFAULT_PREFIX_CACHE_PROMPT_TOKENS,
        help=f"Prompt length for the prefix_cache stage (default {DEFAULT_PREFIX_CACHE_PROMPT_TOKENS}).",
    )
    parser.add_argument(
        "--prefix-cache-output-tokens",
        type=int,
        default=DEFAULT_PREFIX_CACHE_OUTPUT_TOKENS,
        help=f"Greedy output length for the prefix_cache stage (default {DEFAULT_PREFIX_CACHE_OUTPUT_TOKENS}).",
    )
    parser.add_argument(
        "--prefix-cache-resume-suffix-tokens",
        type=int,
        default=DEFAULT_PREFIX_CACHE_RESUME_SUFFIX_TOKENS,
        help=(
            "New-suffix length appended to the shared prefix for the long-resume comparison "
            f"(default {DEFAULT_PREFIX_CACHE_RESUME_SUFFIX_TOKENS}). Needs /reset_prefix_cache, which vLLM "
            "exposes with VLLM_SERVER_DEV_MODE=1."
        ),
    )
    parser.add_argument(
        "--prefix-cache-shared-bench",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also run the shared-prefix `vllm bench serve` workload in the prefix_cache stage.",
    )
    parser.add_argument(
        "--prefill-burst-serial-result",
        type=Path,
        default=None,
        help=(
            "Normalized summary JSON of the same prefill_burst workload measured with batched prefill "
            "disabled. It is read before the burst runs, so the stage's own output path is accepted; "
            "the stage records burst/serial TTFT ratios and whether the workload configs match."
        ),
    )
    return parser


def resolve_output_dir(model_dir: Path, output_dir: Optional[Path]) -> Path:
    return (output_dir if output_dir is not None else model_dir / "readiness_vllm").resolve()


def _main() -> None:
    args = _build_parser().parse_args()

    stages: List[str] = args.stages
    serve_locally = STAGE_SERVE in stages

    if serve_locally and args.server_url is not None:
        parser.error("--server-url is not allowed when `serve` is in --stages")
    if not serve_locally and args.server_url is None:
        parser.error("--server-url is required when `serve` is not in --stages")
    if serve_locally and args.mesh_device is None:
        parser.error("--mesh-device is required when `serve` is in --stages")

    try:
        tt_config = json.loads(args.tt_config)
    except json.JSONDecodeError as e:
        parser.error(f"--tt-config is not valid JSON: {e}")
    if not isinstance(tt_config, dict):
        parser.error(f"--tt-config must be a JSON object, got {type(tt_config).__name__}")
    merged_tt_config: dict[str, Any] = {**DEFAULT_TT_CONFIG, **tt_config}

    additional_server_args = shlex.split(args.additional_server_args) if args.additional_server_args else []
    additional_benchmark_args = shlex.split(args.additional_benchmark_args) if args.additional_benchmark_args else []
    benchmark_temperature = None if args.benchmark_use_server_generation_config else args.benchmark_temperature

    model_dir = args.model_dir.resolve()
    output_dir = resolve_output_dir(model_dir, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    server_log = output_dir / "server.log"
    sampling_log = output_dir / "sampling_tests.log"

    # Reject legacy sources for every local launch, even without sampling.
    if serve_locally:
        _reject_legacy_serving_sources()
    # External-server checks only need the local suite, not a local server stack.
    if STAGE_SAMPLING in stages:
        _find_plugin_tests_dir()

    apc_log_source = server_log if serve_locally else args.server_log
    if not serve_locally and args.enable_prefix_caching is not None:
        parser.error("--enable-prefix-caching only applies when `serve` is in --stages; use --server-log to record an external server")
    run_summary: dict[str, Any] = {
        "stages": stages,
        "hf_model": args.hf_model,
        "server_url": args.server_url,
        "server_command": None,
        "mesh_device": args.mesh_device,
        "max_num_seqs": args.max_num_seqs,
        "max_model_len": args.max_model_len,
        "block_size": args.block_size,
        "tt_config": merged_tt_config,
        "enable_prefix_caching": args.enable_prefix_caching if serve_locally else None,
        "apc_log_enabled": None,
        "apc_log_source": str(apc_log_source) if apc_log_source else None,
        "outputs": {},
    }
    run_summary_file = output_dir / "vllm_run_summary.json"

    server_proc: Optional[subprocess.Popen] = None
    try:
        if serve_locally:
            _check_port_available(args.port)
            run_summary["server_command"] = _server_command(
                hf_model=args.hf_model,
                max_num_seqs=args.max_num_seqs,
                block_size=args.block_size,
                port=args.port,
                max_model_len=args.max_model_len,
                tt_config=merged_tt_config,
                additional_args=additional_server_args,
                enable_prefix_caching=args.enable_prefix_caching,
            )
            server_proc = _launch_server(
                hf_model=args.hf_model,
                mesh_device=args.mesh_device,
                max_num_seqs=args.max_num_seqs,
                block_size=args.block_size,
                port=args.port,
                log_file=server_log,
                max_model_len=args.max_model_len,
                tt_config=merged_tt_config,
                additional_args=additional_server_args,
                enable_prefix_caching=args.enable_prefix_caching,
            )
            _wait_for_server(
                proc=server_proc,
                port=args.port,
                log_file=server_log,
                timeout_seconds=args.server_timeout,
            )
            server_url = f"http://localhost:{args.port}"
        else:
            server_url = args.server_url.rstrip("/")
            _probe_external_server(server_url)

        check_stages = [s for s in stages if s != STAGE_SERVE]

        if serve_locally and not check_stages:
            print(f"\nServer ready at {server_url}")
            _hold_until_signal(server_proc, server_log)
            return

        for stage in check_stages:
            if stage == STAGE_SAMPLING:
                ok = _run_plugin_sampling_tests(
                    server_url=server_url,
                    hf_model=args.hf_model,
                    log_file=sampling_log,
                    server_log=server_log,
                    max_num_seqs=args.max_num_seqs,
                    sampling_profile=args.sampling_profile,
                )
                if not ok:
                    print("\nSampling tests failed — skipping remaining stages.")
                    sys.exit(1)
            elif stage == STAGE_QUALITATIVE:
                _run_qualitative_prompts(
                    server_url=server_url,
                    hf_model=args.hf_model,
                    prompts_file=args.prompts.resolve(),
                    output_dir=output_dir,
                )
            elif stage == STAGE_BENCHMARK:
                primary_summary = _run_serving_benchmark(
                    server_url=server_url,
                    hf_model=args.hf_model,
                    output_dir=output_dir,
                    profile_name="single_user_decode",
                    comparison_scope=(
                        "Primary batch-1 serving decode profile for headline t/s/u. TTFT is reported separately "
                        "and is not blended into the TPOT-derived decode value beyond normal single-request timing."
                    ),
                    raw_result_filename="vllm_result.json",
                    summary_filename="vllm_benchmark.json",
                    log_filename="vllm_benchmark.log",
                    prompt_len=args.benchmark_prompt_len,
                    output_len=args.benchmark_output_len,
                    num_requests=args.benchmark_num_requests,
                    concurrency=args.benchmark_concurrency,
                    temperature=benchmark_temperature,
                    additional_args=additional_benchmark_args,
                )
                if args.benchmark_ci_serving:
                    ci_summary = _run_serving_benchmark(
                        server_url=server_url,
                        hf_model=args.hf_model,
                        output_dir=output_dir,
                        profile_name="ci_serving_burst",
                        comparison_scope=(
                            "vLLM-nightly-style 100/100/32 serving-burst profile for CI parity and serving "
                            "capacity. Do not use as headline decode t/s/u because burst admission and chunked "
                            "prefill can affect TPOT."
                        ),
                        raw_result_filename="vllm_ci_serving_result.json",
                        summary_filename="vllm_ci_serving_benchmark.json",
                        log_filename="vllm_ci_serving_benchmark.log",
                        prompt_len=args.ci_benchmark_prompt_len,
                        output_len=args.ci_benchmark_output_len,
                        num_requests=args.ci_benchmark_num_requests,
                        concurrency=args.ci_benchmark_concurrency,
                        temperature=benchmark_temperature,
                        additional_args=additional_benchmark_args,
                    )
                    primary_summary["secondary_benchmarks"] = {
                        "ci_serving_burst": {
                            "summary_file": str(output_dir / "vllm_ci_serving_benchmark.json"),
                            "raw_result_file": str(output_dir / "vllm_ci_serving_result.json"),
                            "log_file": str(output_dir / "vllm_ci_serving_benchmark.log"),
                            "config": ci_summary["config"],
                            "output_throughput_tok_per_s": ci_summary["output_throughput_tok_per_s"],
                            "vllm_mean_tpot_decode_tps": ci_summary["vllm_mean_tpot_decode_tps"],
                            "comparison_scope": ci_summary["comparison_scope"],
                        }
                    }
                    (output_dir / "vllm_benchmark.json").write_text(json.dumps(primary_summary, indent=2) + "\n")
                run_summary["outputs"]["benchmark"] = str(output_dir / "vllm_benchmark.json")
            elif stage == STAGE_PREFIX_CACHE:
                _run_prefix_cache_check(
                    server_url=server_url,
                    hf_model=args.hf_model,
                    output_dir=output_dir,
                    prompts_file=args.prompts.resolve(),
                    prompt_tokens=args.prefix_cache_prompt_tokens,
                    output_tokens=args.prefix_cache_output_tokens,
                    thresholds=PREFIX_CACHE_THRESHOLDS,
                    server_log=apc_log_source,
                    shared_prefix_bench=args.prefix_cache_shared_bench,
                    additional_benchmark_args=additional_benchmark_args,
                    resume_suffix_tokens=args.prefix_cache_resume_suffix_tokens,
                )
                run_summary["outputs"]["prefix_cache"] = str(output_dir / "prefix_cache_check.json")
            elif stage == STAGE_PREFILL_BURST:
                serial_path = args.prefill_burst_serial_result.resolve() if args.prefill_burst_serial_result else None
                serial = json.loads(serial_path.read_text()) if serial_path else None
                burst_summary = _run_serving_benchmark(
                    server_url=server_url,
                    hf_model=args.hf_model,
                    output_dir=output_dir,
                    profile_name="prefill_burst",
                    comparison_scope=(
                        "32 short prompts submitted at once so the scheduler forms multi-row prefill steps. "
                        "Compare TTFT against the same workload with batched prefill disabled; not a decode number."
                    ),
                    raw_result_filename="vllm_prefill_burst_result.json",
                    summary_filename="vllm_prefill_burst_benchmark.json",
                    log_filename="vllm_prefill_burst_benchmark.log",
                    prompt_len=DEFAULT_BURST_PROMPT_LEN,
                    output_len=DEFAULT_BURST_OUTPUT_LEN,
                    num_requests=DEFAULT_BURST_NUM_REQUESTS,
                    concurrency=DEFAULT_BURST_CONCURRENCY,
                    temperature=0.0,
                    additional_args=additional_benchmark_args,
                )
                _attach_serial_reference(
                    output_dir / "vllm_prefill_burst_benchmark.json", burst_summary, serial, serial_path
                )
                run_summary["outputs"]["prefill_burst"] = str(output_dir / "vllm_prefill_burst_benchmark.json")

        print("\n" + "=" * 60)
        print(f"vLLM checks complete. Results in {output_dir}")
        print("=" * 60)
    finally:
        if server_proc is not None:
            _shutdown(server_proc, server_log)
        run_summary["apc_log_enabled"] = _server_log_apc_state(apc_log_source)
        _write_run_summary(run_summary_file, run_summary)
        print(f"Run summary: {run_summary_file}")


if __name__ == "__main__":
    _main()
