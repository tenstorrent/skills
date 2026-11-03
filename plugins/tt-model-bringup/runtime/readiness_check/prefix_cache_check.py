# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Served prefix-cache check: counter deltas, TTFT, greedy agreement, long resume and shared-prefix bench."""

import json
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

import requests
from transformers import AutoTokenizer

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


def run_prefix_cache_check(
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
    run_serving_benchmark: Callable[..., dict[str, Any]],
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
        summary = run_serving_benchmark(
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
