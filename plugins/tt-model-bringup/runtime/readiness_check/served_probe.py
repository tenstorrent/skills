# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Stdlib helpers shared by the served eval-readiness probes (P4 long answers, P5 long context).

The probes attach to a running vLLM server. They use only the standard library so
they run from any interpreter that can reach the server. The Prometheus parser and
the fatal log markers match ``prefix_cache_check._parse_prometheus_counters`` and
``run_vllm_server._FATAL_LOG_PATTERNS``; a test keeps them in step.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

PROFILES_PATH = Path(__file__).with_name("eval_request_profiles.json")
# Used when p23's profile file is absent: the common Qwen-style reasoning settings.
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "default": {
        "name": "default",
        "source": "built-in fallback; replace with the model card or eval config",
        "chat": True,
        "thinking": True,
        "request": {"temperature": 0.6, "top_p": 0.95, "top_k": 20},
    },
    # Mirror of eval_request_profiles.json `lm_eval_reasoning` (the P4/P5 default), used only
    # when that file is absent or overridden by a missing --profiles-file.
    "lm_eval_reasoning": {
        "name": "lm_eval_reasoning",
        "source": "built-in mirror of eval_request_profiles.json lm_eval_reasoning",
        "chat": True,
        "thinking": True,
        "request": {"temperature": 1.0, "top_p": 0.95, "top_k": 40, "seed": 42},
    },
    # Reference for the host-sampling comparison: settings every device sampler supports.
    "plain": {
        "name": "plain",
        "source": "built-in reference: greedy, no seed",
        "chat": True,
        "thinking": True,
        "request": {"temperature": 0.0},
    },
}

FATAL_LOG_PATTERNS = (
    "EngineCore failed to start",
    "EngineCore encountered a fatal error",
    "EngineDeadError",
    "Engine core initialization failed",
    "Failed core proc",
)
# vLLM error lines worth surfacing after a load (request-level 4xx are logged as INFO).
ERROR_LINE_RE = re.compile(r"\bERROR\b|Traceback \(most recent call last\)")
# Default trace-capture marker. Generators log their own capture events (see the
# vllm-integration mixed-shape trace reuse check, step 6); pass the model's pattern
# with --trace-capture-regex when it logs something else.
DEFAULT_TRACE_CAPTURE_RE = r"(?i)(begin_trace_capture|recaptur|captur(e|ed|ing)\b[^\n]{0,40}\btrace|trace\b[^\n]{0,40}\bcaptur)"
SAMPLE_MODE_RE = re.compile(r"sample_on_device_mode['\"]?\s*[=:]\s*['\"]?([A-Za-z_]+)")
MNBT_RE = re.compile(r"max_num_batched_tokens['\"]?\s*[=:]\s*(\d+)")
KV_POOL_LOG_RE = re.compile(r"KV cache size:\s*([\d,]+)\s*tokens")


class ProbeError(Exception):
    """The probe could not run (server unreachable, bad input). Exit 3."""


# ---------------------------------------------------------------- HTTP


def http_json(url: str, body: Optional[dict] = None, *, timeout: float = 60.0, method: Optional[str] = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def http_status(url: str, *, method: str = "GET", timeout: float = 10.0) -> Optional[int]:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def check_health(server_url: str) -> None:
    status = http_status(f"{server_url}/health")
    if status != 200:
        raise ProbeError(f"server not healthy at {server_url}/health (status {status}); start it first")


def served_model(server_url: str) -> tuple[str, Optional[int]]:
    """Return (model id, max_model_len) from /v1/models."""
    data = http_json(f"{server_url}/v1/models", timeout=30)
    rows = (data or {}).get("data") or []
    if not rows:
        raise ProbeError(f"{server_url}/v1/models lists no model")
    row = rows[0]
    max_len = row.get("max_model_len")
    return str(row.get("id")), int(max_len) if isinstance(max_len, (int, float)) else None


def tokenize(server_url: str, model: str, *, prompt: Optional[str] = None, messages: Optional[list] = None,
             chat_kwargs: Optional[dict] = None) -> list[int]:
    """Token ids from vLLM's /tokenize, so counts include the served chat template."""
    body: dict[str, Any] = {"model": model}
    if messages is not None:
        body.update(messages=messages, add_generation_prompt=True)
        if chat_kwargs:
            body["chat_template_kwargs"] = chat_kwargs
    else:
        body.update(prompt=prompt or "", add_special_tokens=False)
    try:
        data = http_json(f"{server_url}/tokenize", body, timeout=300)
    except (urllib.error.URLError, OSError) as exc:
        raise ProbeError(f"/tokenize failed ({exc}); the probe needs vLLM's tokenizer endpoint") from exc
    return list(data.get("tokens") or [])


def reset_prefix_cache(server_url: str) -> bool:
    """Best effort; vLLM exposes it with VLLM_SERVER_DEV_MODE=1."""
    return http_status(f"{server_url}/reset_prefix_cache", method="POST", timeout=60) == 200


def stream_request(
    url: str,
    body: dict,
    *,
    timeout: float = 7200.0,
    stop: Optional[threading.Event] = None,
    keep_text: bool = True,
) -> dict[str, Any]:
    """Stream one OpenAI request and time every content chunk.

    Returns start/first/end times (perf_counter), per-chunk times, text, reasoning
    text, the usage block when the server sends one, the finish reason and any error.
    """
    body = dict(body, stream=True)
    body.setdefault("stream_options", {"include_usage": True})
    rec: dict[str, Any] = {"t0": time.perf_counter(), "times": [], "text": "", "reasoning": "",
                           "usage": None, "finish_reason": None, "error": None, "aborted": False}
    parts: list[str] = []
    reasoning: list[str] = []
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                if stop is not None and stop.is_set():
                    rec["aborted"] = True
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    rec["error"] = str(chunk["error"])[:300]
                    break
                if chunk.get("usage"):
                    rec["usage"] = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta")
                    piece = choice.get("text") if delta is None else (delta.get("content") or "")
                    think = "" if delta is None else (delta.get("reasoning_content") or delta.get("reasoning") or "")
                    if piece or think:
                        rec["times"].append(time.perf_counter())
                        if keep_text:
                            parts.append(piece or "")
                            reasoning.append(think)
                    if choice.get("finish_reason"):
                        rec["finish_reason"] = choice["finish_reason"]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300] if exc.fp else ""
        rec["error"] = f"HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if not rec["aborted"]:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
    rec["t_end"] = time.perf_counter()
    rec["text"] = "".join(parts)
    rec["reasoning"] = "".join(reasoning)
    if not rec["error"] and not rec["aborted"] and rec["finish_reason"] is None:
        rec["error"] = "stream ended without a finish_reason"
    return rec


def output_tokens(rec: dict) -> int:
    usage = rec.get("usage") or {}
    if isinstance(usage.get("completion_tokens"), int):
        return usage["completion_tokens"]
    return len(rec.get("times") or [])


def token_times(rec: dict) -> list[float]:
    """Per-token arrival times, stretched over chunks when the server sends several tokens per chunk."""
    times = rec.get("times") or []
    n = output_tokens(rec)
    if not times or n <= len(times):
        return list(times)
    out = []
    for i in range(n):
        out.append(times[min(len(times) - 1, int(i * len(times) / n))])
    return out


def ttft_ms(rec: dict) -> Optional[float]:
    times = rec.get("times") or []
    return (times[0] - rec["t0"]) * 1000.0 if times else None


def tpot_ms(rec: dict) -> Optional[float]:
    times = rec.get("times") or []
    n = output_tokens(rec)
    if len(times) < 2 or n < 2:
        return None
    return (times[-1] - times[0]) * 1000.0 / (n - 1)


def max_gap_ms(rec: dict, start: float, end: float) -> Optional[float]:
    """Largest gap between consecutive chunks that overlaps [start, end]."""
    times = rec.get("times") or []
    gaps = [(b - a) * 1000.0 for a, b in zip(times, times[1:]) if b >= start and a <= end]
    return max(gaps) if gaps else None


def run_concurrent(bodies: list[tuple[str, dict]], **kwargs) -> list[dict]:
    """Start every request at once (one thread each) and return the records in order."""
    results: list[Optional[dict]] = [None] * len(bodies)
    barrier = threading.Barrier(len(bodies)) if bodies else None

    def worker(index: int, url: str, body: dict) -> None:
        barrier.wait()
        results[index] = stream_request(url, body, **kwargs)

    threads = [threading.Thread(target=worker, args=(i, url, body), daemon=True) for i, (url, body) in enumerate(bodies)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [r or {"error": "worker did not run"} for r in results]


# ---------------------------------------------------------------- request bodies


def load_profile(name: str, path: Optional[Path] = None) -> dict:
    """Read one profile from eval_request_profiles.json, else the built-in fallback."""
    path = path or PROFILES_PATH
    if path.is_file():
        data = json.loads(path.read_text())
        for profile in data.get("profiles") or []:
            if profile.get("name") == name:
                return profile
        if name not in BUILTIN_PROFILES:
            names = [p.get("name") for p in data.get("profiles") or []]
            raise ProbeError(f"profile {name!r} not in {path}; known: {names}")
    if name in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[name]
    raise ProbeError(f"{path} is missing and {name!r} is not a built-in profile ({sorted(BUILTIN_PROFILES)})")


def profile_ignores_eos(profile: dict) -> bool:
    return bool(profile.get("ignore_eos") or (profile.get("request") or {}).get("ignore_eos"))


def build_request(
    server_url: str,
    model: str,
    profile: dict,
    *,
    content: Any,
    max_tokens: int,
    overrides: Optional[dict] = None,
) -> tuple[str, dict]:
    """(url, body) for one request. ``content`` is a chat message string, or token ids / text for completions."""
    params = {k: v for k, v in (profile.get("request") or {}).items() if k not in ("max_tokens", "max_completion_tokens", "stream", "n")}
    params.update(overrides or {})
    if profile.get("chat", True) and isinstance(content, str):
        body = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens, **params}
        kwargs = dict(body.get("chat_template_kwargs") or {})
        if "thinking" in profile:
            kwargs.setdefault("enable_thinking", bool(profile["thinking"]))
        if kwargs:
            body["chat_template_kwargs"] = kwargs
        return f"{server_url}/v1/chat/completions", body
    body = {"model": model, "prompt": content, "max_tokens": max_tokens, **params}
    body.pop("chat_template_kwargs", None)
    return f"{server_url}/v1/completions", body


# ---------------------------------------------------------------- /metrics


def parse_prometheus(text: str) -> tuple[dict[str, float], list[tuple[str, dict[str, str], float]]]:
    """Sum every sample of each metric (``_total`` folded into the base name) and keep labelled samples."""
    totals: dict[str, float] = {}
    samples: list[tuple[str, dict[str, str], float]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name_part, _, value_part = line.rpartition(" ")
        if not name_part or not value_part:
            continue
        name, _, label_text = name_part.partition("{")
        if name.endswith("_total"):
            name = name[: -len("_total")]
        try:
            value = float(value_part)
        except ValueError:
            continue
        totals[name] = totals.get(name, 0.0) + value
        labels = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', label_text))
        samples.append((name, labels, value))
    return totals, samples


KV_USAGE_NAMES = ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")


def metrics_snapshot(server_url: str) -> Optional[dict[str, Any]]:
    """The gauges and counters the probes read, or None when /metrics is unavailable."""
    try:
        req = urllib.request.Request(f"{server_url}/metrics")
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return None
    totals, samples = parse_prometheus(text)
    kv = next((totals[name] for name in KV_USAGE_NAMES if name in totals), None)
    snap: dict[str, Any] = {
        "t": time.perf_counter(),
        "running": totals.get("vllm:num_requests_running"),
        "waiting": totals.get("vllm:num_requests_waiting"),
        "kv_usage": kv,
        "preemptions": totals.get("vllm:num_preemptions"),
        "prompt_tokens": totals.get("vllm:prompt_tokens"),
        "generation_tokens": totals.get("vllm:generation_tokens"),
    }
    for name, labels, _ in samples:
        if name == "vllm:cache_config_info" and labels.get("num_gpu_blocks") and labels.get("block_size"):
            try:
                snap["kv_pool_tokens"] = int(labels["num_gpu_blocks"]) * int(labels["block_size"])
            except ValueError:
                pass
    return snap


class MetricsSampler:
    """Poll /metrics on a thread while a load runs."""

    def __init__(self, server_url: str, interval_s: float = 2.0):
        self.server_url = server_url
        self.interval_s = interval_s
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            snap = metrics_snapshot(self.server_url)
            if snap is not None:
                self.samples.append(snap)
            self._stop.wait(self.interval_s)

    def __enter__(self) -> "MetricsSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=60)


# ---------------------------------------------------------------- server log


class ServerLog:
    """Read what the server logged after a mark, so a probe judges only its own window."""

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None

    @property
    def available(self) -> bool:
        return bool(self.path and self.path.is_file())

    def size(self) -> int:
        return self.path.stat().st_size if self.available else 0

    def text(self, start: int = 0, end: Optional[int] = None) -> str:
        if not self.available:
            return ""
        with open(self.path, "rb") as handle:
            handle.seek(start)
            data = handle.read() if end is None else handle.read(max(0, end - start))
        return data.decode("utf-8", errors="replace")


def fatal_markers(text: str) -> list[str]:
    return [p for p in FATAL_LOG_PATTERNS if p in text]


def error_lines(text: str, limit: int = 5) -> list[str]:
    lines = [line.strip()[:200] for line in text.splitlines() if ERROR_LINE_RE.search(line)]
    return lines[:limit]


def count_matches(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text)) if text else 0


def sample_on_device_mode(text: str) -> Optional[str]:
    found = SAMPLE_MODE_RE.findall(text)
    return found[-1] if found else None


def max_num_batched_tokens(text: str) -> Optional[int]:
    found = MNBT_RE.findall(text)
    return int(found[-1]) if found else None


def kv_pool_tokens_from_log(text: str) -> Optional[int]:
    found = KV_POOL_LOG_RE.findall(text)
    return int(found[-1].replace(",", "")) if found else None


# ---------------------------------------------------------------- misc


def median(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if isinstance(v, (int, float))]
    return statistics.median(vals) if vals else None


def parse_csv_ints(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def parse_csv_floats(text: str) -> list[float]:
    return [float(x) for x in text.split(",") if x.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def r(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(value, digits) if isinstance(value, (int, float)) else None


def wait_until(predicate: Callable[[], bool], timeout_s: float, interval_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)
