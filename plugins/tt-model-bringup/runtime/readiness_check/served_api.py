# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Small stdlib helpers for probes that talk to a running vLLM OpenAI server.

Used by the eval-readiness request probes (``run_harness_requests``,
``run_chat_conformance``). Standard library only, so the probes import and
test without ``requests`` or ``openai``.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

DEFAULT_TIMEOUT_S = 600.0

# Tags that must never reach `content` once a reasoning parser splits the
# output. Extended at run time with the tokenizer's own thinking tokens.
THINKING_TAGS = (
    "<think>",
    "</think>",
    "<thinking>",
    "</thinking>",
    "<|channel|>",
    "<|start_thinking|>",
    "<|end_thinking|>",
    "<|begin_of_thought|>",
    "<|end_of_thought|>",
    "<seed:think>",
    "</seed:think>",
)


def http_json(
    method: str, url: str, payload: Optional[dict] = None, *, timeout: float = DEFAULT_TIMEOUT_S
) -> tuple[int, Any, float]:
    """Send one request; return (status, parsed body or text, elapsed seconds). Status 0 is a transport error."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}", time.perf_counter() - started
    elapsed = time.perf_counter() - started
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text), elapsed
    except json.JSONDecodeError:
        return status, text, elapsed


def error_text(body: Any, limit: int = 240) -> str:
    """One-line server error message from an OpenAI-style error body."""
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            body = err.get("message") or err
        else:
            body = err
    text = body if isinstance(body, str) else json.dumps(body)
    text = " ".join(text.split())
    return text[:limit]


def stream_post(url: str, payload: dict, *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """POST with ``stream: true``; return status, parsed chunks and their arrival times (seconds from start)."""
    body = dict(payload, stream=True)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    started = time.perf_counter()
    chunks: list[tuple[float, dict]] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunks.append((time.perf_counter() - started, json.loads(data)))
                except json.JSONDecodeError:
                    continue
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "error": error_text(_safe_json(exc.read())), "chunks": []}
    except (urllib.error.URLError, OSError) as exc:
        return {"status": 0, "error": f"{type(exc).__name__}: {exc}", "chunks": chunks}
    error = None
    for _, chunk in chunks:
        if isinstance(chunk, dict) and chunk.get("error"):
            error = error_text(chunk)
    return {"status": status, "error": error, "chunks": chunks}


def _safe_json(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def delta_text(chunk: dict) -> tuple[str, str]:
    """(content, reasoning) text carried by one streamed chunk, chat or completion."""
    content, reasoning = "", ""
    for choice in chunk.get("choices") or []:
        if "text" in choice and isinstance(choice.get("text"), str):
            content += choice["text"]
        delta = choice.get("delta") or {}
        content += delta.get("content") or ""
        reasoning += delta.get("reasoning_content") or delta.get("reasoning") or ""
    return content, reasoning


def stream_timing(result: dict[str, Any]) -> dict[str, Any]:
    """Per-request decode timing from a streamed response.

    TPOT is (last token chunk - first token chunk) / (tokens - 1). The token
    count comes from the final usage block when the server sends it, else the
    number of chunks that carried text.
    """
    times = []
    usage_tokens = None
    for t, chunk in result.get("chunks") or []:
        usage = chunk.get("usage") if isinstance(chunk, dict) else None
        if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
            usage_tokens = int(usage["completion_tokens"])
        content, reasoning = delta_text(chunk)
        if content or reasoning:
            times.append(t)
    tokens = usage_tokens if usage_tokens is not None else len(times)
    out: dict[str, Any] = {"tokens": tokens, "ttft_ms": times[0] * 1000.0 if times else None, "tpot_ms": None}
    if len(times) >= 2 and tokens >= 2:
        out["tpot_ms"] = (times[-1] - times[0]) * 1000.0 / (tokens - 1)
    return out


def served_models(server_url: str, *, timeout: float = 30.0) -> list[dict]:
    status, body, _ = http_json("GET", f"{server_url.rstrip('/')}/v1/models", timeout=timeout)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"GET /v1/models returned {status}: {error_text(body)}; is the server up at {server_url}?")
    return [m for m in body.get("data") or [] if isinstance(m, dict)]


def resolve_served_model(server_url: str, requested: Optional[str]) -> tuple[str, Optional[int]]:
    """Return (model id to send, served max_model_len if the server reports it)."""
    models = served_models(server_url)
    if not models:
        raise RuntimeError("the server lists no models")
    chosen = next((m for m in models if m.get("id") == requested), None) if requested else None
    chosen = chosen or models[0]
    max_len = chosen.get("max_model_len")
    return (requested or str(chosen.get("id"))), (int(max_len) if isinstance(max_len, int) else None)


def parse_prometheus(text: str) -> dict[str, float]:
    """Sum every sample of each metric; `_total` folds into the base name."""
    totals: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name_part, _, value_part = line.rpartition(" ")
        name = name_part.split("{", 1)[0]
        if name.endswith("_total"):
            name = name[: -len("_total")]
        try:
            totals[name] = totals.get(name, 0.0) + float(value_part)
        except ValueError:
            continue
    return totals


TPOT_METRIC_NAMES = (
    "vllm:request_time_per_output_token_seconds",
    "vllm:time_per_output_token_seconds",
)


def fetch_tpot_counters(server_url: str) -> Optional[dict[str, float]]:
    """Sum/count of the server's per-output-token histogram, or None when /metrics lacks it."""
    status, body, _ = http_json("GET", f"{server_url.rstrip('/')}/metrics", timeout=30.0)
    if status != 200 or not isinstance(body, str):
        return None
    values = parse_prometheus(body)
    for name in TPOT_METRIC_NAMES:
        if f"{name}_sum" in values and f"{name}_count" in values:
            return {"metric": name, "sum": values[f"{name}_sum"], "count": values[f"{name}_count"]}
    return None


def metrics_tpot_ms(before: Optional[dict], after: Optional[dict]) -> Optional[float]:
    if not before or not after or before.get("metric") != after.get("metric"):
        return None
    count = after["count"] - before["count"]
    if count <= 0:
        return None
    return (after["sum"] - before["sum"]) * 1000.0 / count


class LogCursor:
    """Read only the server-log lines written since the last call."""

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self.offset = self.path.stat().st_size if self.path and self.path.is_file() else 0

    def read_new(self) -> str:
        if not self.path or not self.path.is_file():
            return ""
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read()
        self.offset += len(data)
        return data.decode("utf-8", errors="replace")


_ARG_PATTERNS = {
    "reasoning_parser": r"reasoning[-_]parser",
    "reasoning_parser_plugin": r"reasoning[-_]parser[-_]plugin",
    "tool_call_parser": r"tool[-_]call[-_]parser",
    "tool_parser_plugin": r"tool[-_]parser[-_]plugin",
    "chat_template": r"chat[-_]template",
    "chat_template_content_format": r"chat[-_]template[-_]content[-_]format",
    "enable_auto_tool_choice": r"enable[-_]auto[-_]tool[-_]choice",
    "default_chat_template_kwargs": r"default[-_]chat[-_]template[-_]kwargs",
    "generation_config": r"generation[-_]config",
    "override_generation_config": r"override[-_]generation[-_]config",
    "seeded_sampling_policy": r"seeded_sampling_policy",
    "sample_on_device_mode": r"sample_on_device_mode",
}


def parse_server_settings(text: str) -> dict[str, Any]:
    """Pull parser and sampling settings from a server command line or log.

    Understands ``--flag value`` on a command line and the ``'flag': value``
    form vLLM prints in its "non-default args" log line and the TT plugin's
    additional config.
    """
    found: dict[str, Any] = {}
    if not text:
        return found
    for key, pat in _ARG_PATTERNS.items():
        cli = re.search(rf"--{pat}(?![-_\w])(?:[= ]+(?!--)(\S+))?", text)
        if cli:
            value = cli.group(1)
            found[key] = True if value is None else value.strip("'\"")
            continue
        logged = re.search(rf"['\"]{pat}['\"]\s*:\s*('[^']*'|\"[^\"]*\"|[^,}}\s]+)", text)
        if logged:
            value = logged.group(1).strip("'\"")
            found[key] = {"True": True, "true": True, "False": False, "false": False, "None": None}.get(value, value)
    return found


def read_text(path: Optional[Path]) -> str:
    if path and Path(path).is_file():
        return Path(path).read_text(errors="replace")
    return ""
