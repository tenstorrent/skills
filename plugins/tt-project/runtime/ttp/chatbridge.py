# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Messages-to-Chat-Completions bridge: Claude Code workers on an OpenAI-compatible endpoint.

The `chat` provider (providers/chat.py) runs Claude Code against this bridge. The runner starts one
per run, in its own process (a thread, so it can never outlive the run: a normal end, a timeout, a
kill or a reboot ends it with the runner), bound to 127.0.0.1 on an ephemeral port. It serves the
part of the Anthropic Messages API Claude Code uses (POST /v1/messages, streamed or not, and an
estimate for /v1/messages/count_tokens) and forwards each call to <base_url>/chat/completions.

The upstream key is read from the `ttp secret endpoint <alias>` slot by the runner and lives only in
this object and the upstream Authorization header: never argv, the run's files, logs or the agent's
environment. The agent gets a per-run bridge token instead, which the bridge checks, so no other
local process can spend the key through the port.

Mapping: system text, user and assistant text, tool definitions and choice, tool_use and
tool_result blocks (parallel calls matched by id), stop reasons and usage. Reasoning (thinking
blocks out, reasoning_content in) is dropped; max_tokens is capped from the provider entry.
Upstream errors come back as Messages errors the runner already classifies: 401/403 as an
authentication error (the auth gate), 402 as a funds limit, 429 as a rate limit, 503/529 as
overloaded and other 5xx as a retryable api_error. A prompt past the model's context window comes
back worded as Anthropic's ('prompt is too long: N tokens > M maximum'), which Claude Code compacts
on; a tier's context_window also sets the agent's CLAUDE_CODE_AUTO_COMPACT_WINDOW, as Claude Code
takes 200k for a model name it does not know.

Effort: `providers.chat.effort_fields.<model>.<effort>` (or its `*` fallback) names extra request
fields for the effort the daemon launched the run at (low, medium, high, xhigh, max), such as
{"reasoning_effort": "low"} or {"enable_thinking": false}; they win over Claude Code's own and never
replace a field that carries the conversation (CONVERSATION). Unset, a request goes out as it is. An
endpoint that rejects one (a 400 naming it) gets the request again without them, and the rest of the
run goes without them; stats() records it for the run's meter.

Mid-run model swap: a run whose launch named ranked stand-ins (`standins`, modelrank.standins: the
ranking's other models, none for a required pin) keeps going when its model goes away. When the
upstream refuses the model (model_missing: a 404 naming it or one /models leaves out; or the error
code model_not_found or unrecognized_model), or SWAP_ERRORS calls in a row end in a 5xx or no answer
while the served lists (`served`: state dirs holding route.MODELS_FILE, read fresh, modelrank.served)
no longer list it, that call and every later one go to the first stand-in those lists name that this
run has not left. Its effort_fields and context window are resolved again; the session and worktree
stay. CLAUDE_CODE_AUTO_COMPACT_WINDOW is set once at start, so a stand-in with a smaller window
relies on the 'prompt is too long' answer above for Claude Code to compact. stats() lists each swap
(`mid_run`: {from, to, at, why}); the daemon keeps it in the run note (model_rank.mid_run) with one
low alert per swap. No stand-in served (or the lists unknown): the error goes back as before.
"""
from __future__ import annotations

import hmac
import http.server
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import endpoint, redact

UPSTREAM_TIMEOUT_S = 600
MODEL_CHECK_TIMEOUT_S = 10   # the /models check a bare 404 gets (_model_listed)
STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                "function_call": "tool_use", "content_filter": "end_turn"}
# An upstream's prompt-too-long answers: OpenAI's code and the wordings of common servers (OpenAI,
# vLLM, llama.cpp, TGI and the like). Matched on the error body, its code included.
CONTEXT_RE = re.compile(
    r"context_length_exceeded|exceed_context_size|maximum context length|context length (?:is |was )?exceeded"
    r"|exceeds? (?:the )?(?:available |maximum |model'?s? )*context|too many (?:input |prompt )?tokens"
    r"|prompt is too long|input is too long|tokens \+ `?max_new_tokens`? must be", re.I)
CONTEXT_STATUSES = (400, 413, 422)
EFFORTS = ("low", "medium", "high", "xhigh", "max")   # the run efforts an effort_fields entry may name
# Request fields that carry the conversation or the bridge's own wiring: effort_fields never sets them.
CONVERSATION = frozenset({"model", "messages", "tools", "tool_choice", "parallel_tool_calls", "functions",
                          "function_call", "stream", "stream_options", "n", "max_tokens"})
REJECT_STATUSES = (400, 422)
REFUSAL_CODES = ("model_not_found", "unrecognized_model")   # error codes that refuse the run's model
SWAP_FILE = "stand-in.json"   # in the run dir: {asked, ran} once a mid-run swap ran a stand-in (live views)
SWAP_ERRORS = 3   # 5xx or no-answer calls in a row that swap a model the served lists no longer name


class BridgeError(Exception):
    """The bridge cannot start: no key saved, or the config names another URL than the key's."""
    auth = False


class BridgeAuthError(BridgeError):
    """No usable key for the endpoint: the provider's auth gate, not a launch failure (the runner
    records it as `launch_auth`, the daemon books the run `auth`)."""
    auth = True


def settings(cfg: dict, tier: str, model: str, effort: str = "", standins=(), served=()) -> dict:
    """The run.json `bridge` entry for a `chat` run: no key, only where to find it, and the extra
    request fields for the run's effort (effort_fields) when its model has any. `standins` (ranked
    model ids) and `served` (state dirs with a served list) arm the mid-run swap, each stand-in with
    its own effort fields and context window."""
    entry = ((cfg or {}).get("providers") or {}).get("chat") or {}
    row = (entry.get("tiers") or {}).get(tier) or {}
    cap = row.get("max_tokens") or entry.get("max_tokens") or 0
    window = window_of(entry, model, row)
    fields = effort_fields(entry, model, effort)
    alts = [{"model": m, **({"fields": f} if (f := effort_fields(entry, m, effort)) else {}),
             **({"context_window": w} if (w := window_of(entry, m)) else {})} for m in standins if m != model]
    return {"endpoint": str(entry.get("endpoint") or ""), "base_url": str(entry.get("base_url") or ""),
            "model": model, "max_tokens": int(cap or 0), **({"context_window": int(window)} if window else {}),
            **({"effort": effort, "fields": fields} if fields else {}),
            **({"effort": effort, "standins": alts, "served": [str(d) for d in served]} if alts and served else {})}


def window_of(entry: dict, model: str, row: dict | None = None) -> int:
    """The context window for `model`: the run's tier row's (`row`), else a tier row naming that model,
    else providers.chat.context_window; 0 for Claude Code's own."""
    rows = [row or {}] + [r for r in ((entry or {}).get("tiers") or {}).values()
                          if isinstance(r, dict) and r.get("model") == model]
    for r in rows:
        if r.get("context_window"):
            return int(r["context_window"])
    return int((entry or {}).get("context_window") or 0)


def effort_fields(entry: dict, model: str, effort: str) -> dict:
    """The request fields providers.chat.effort_fields gives `model` at `effort` (its `*` entry when
    the effort has none), or {}: unset or invalid, the request goes out unchanged."""
    table = (entry or {}).get("effort_fields") if isinstance(entry, dict) else None
    if not isinstance(table, dict) or _effort_problems(table):
        return {}
    row = table.get(model) or {}
    got = row.get(effort) if effort in row else row.get("*")
    return dict(got or {})


def _effort_problems(table) -> list[str]:
    if not isinstance(table, dict):
        return ["providers.chat.effort_fields: give an object of model names, each an object of efforts; "
                "no fields are sent"]
    out = []
    for model, row in table.items():
        where = f"providers.chat.effort_fields.{model}"
        if not isinstance(row, dict):
            out.append(f"{where}: give an object of efforts ({', '.join(EFFORTS)} or *); no fields are sent")
            continue
        for effort, fields in row.items():
            if effort not in (*EFFORTS, "*"):
                out.append(f"{where}.{effort}: not an effort; give {', '.join(EFFORTS)} or *; no fields are sent")
            elif not isinstance(fields, dict):
                out.append(f"{where}.{effort}: give an object of request fields, such as "
                           '{"reasoning_effort": "low"}; no fields are sent')
            elif bad := sorted(CONVERSATION & set(fields)):
                out.append(f"{where}.{effort}: {', '.join(bad)} carries the conversation and cannot be set; "
                           "no fields are sent")
    return out


def with_fields(req: dict, fields: dict) -> dict:
    """`req` with the effort fields set over its own (Claude Code's), never a CONVERSATION field."""
    return {**req, **{k: v for k, v in (fields or {}).items() if k not in CONVERSATION}}


def rejects(status: int, detail: str, fields: dict) -> bool:
    """Whether an upstream error refuses one of the effort fields: a 400 (or 422) naming it, not a
    prompt past the window."""
    low = detail.lower()
    return (status in REJECT_STATUSES and bool(fields) and not too_long(detail)
            and any(k.lower() in low for k in fields))


def config_problems(raw: dict) -> list[str]:
    """What is wrong with project.json's providers.chat entry (none when it has none)."""
    entry = (raw.get("providers") or {}).get("chat") if isinstance(raw.get("providers"), dict) else None
    if entry is None:
        return []
    if not isinstance(entry, dict):
        return ["providers.chat: not an object; chat runs cannot start"]
    out = _effort_problems(entry["effort_fields"]) if entry.get("effort_fields") is not None else []
    try:
        endpoint.slot(str(entry.get("endpoint") or ""))
    except ValueError:
        out.append("providers.chat.endpoint: name the `ttp secret endpoint <alias>` that holds the key; "
                   "chat runs cannot start without it")
    if entry.get("base_url"):
        try:
            endpoint.base_url(str(entry["base_url"]))
        except ValueError as e:
            out.append(f"providers.chat.base_url: {e}")
    for key, unset in (("max_tokens", "no cap"), ("context_window", "Claude Code's own")):
        caps = [entry.get(key), *((t or {}).get(key) for t in (entry.get("tiers") or {}).values()
                                  if isinstance(t, dict))]
        if any(c is not None and (isinstance(c, bool) or not isinstance(c, int) or c < 0) for c in caps):
            out.append(f"providers.chat {key}: give a whole number of tokens (0 = {unset})")
    rows = ((raw.get("pricing") or {}) if isinstance(raw.get("pricing"), dict) else {}).get("chat")
    if rows is not None and (not isinstance(rows, dict) or not all(price_ok(v) for v in rows.values())):
        out.append("pricing.chat: give each model (or \"default\") [input, cached input, output] in $ per "
                   "million tokens; chat spend is estimated until then")
    return out


def price_ok(row) -> bool:
    return (isinstance(row, (list, tuple)) and len(row) == 3
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 for x in row))


def _text(content) -> str:
    """The text of a Messages content field (a string or a list of blocks); other blocks dropped."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(str(b.get("text") or ""))
        elif isinstance(b, dict) and b.get("type") == "image":
            out.append("[image omitted]")
    return "\n".join(out)


def _image_part(b: dict) -> dict | None:
    src = b.get("source") or {}
    if src.get("type") == "base64":
        return {"type": "image_url", "image_url": {"url": f"data:{src.get('media_type')};base64,{src.get('data')}"}}
    if src.get("type") == "url":
        return {"type": "image_url", "image_url": {"url": src.get("url")}}
    return None


def to_chat(body: dict, model: str, cap: int = 0) -> dict:
    """A Chat Completions request for a Messages request, sent to `model` with max_tokens at most `cap`."""
    msgs: list[dict] = []
    system = _text(body.get("system") or "")
    if system:
        msgs.append({"role": "system", "content": system})
    for m in body.get("messages") or []:
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        if m.get("role") == "assistant":
            text = "".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
            calls = [{"id": b.get("id"), "type": "function",
                      "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            msg: dict = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            if text or calls:
                msgs.append(msg)
            continue
        # A user turn: its tool results first (each answers a call of the turn before), then the rest.
        parts: list[dict] = []
        for b in blocks:
            kind = b.get("type")
            if kind == "tool_result":
                out = _text(b.get("content") or "")
                msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                             "content": ("Error: " + out) if b.get("is_error") else out})
            elif kind == "text":
                parts.append({"type": "text", "text": str(b.get("text") or "")})
            elif kind == "image" and (img := _image_part(b)):
                parts.append(img)
        if parts:
            only_text = all(p["type"] == "text" for p in parts)
            msgs.append({"role": "user", "content": "\n".join(p["text"] for p in parts) if only_text else parts})
    req: dict = {"model": model, "messages": msgs}
    want = int(body.get("max_tokens") or 0)
    if want or cap:
        req["max_tokens"] = min(want, cap) if want and cap else (want or cap)
    tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                               "parameters": t["input_schema"]}}
             for t in body.get("tools") or [] if isinstance(t, dict) and t.get("input_schema")]
    if tools:
        req["tools"] = tools
        choice = body.get("tool_choice") or {}
        kind = choice.get("type")
        if kind == "any":
            req["tool_choice"] = "required"
        elif kind == "tool":
            req["tool_choice"] = {"type": "function", "function": {"name": choice.get("name")}}
        elif kind == "none":
            req["tool_choice"] = "none"
        if choice.get("disable_parallel_tool_use"):
            req["parallel_tool_calls"] = False
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("stop_sequences", "stop")):
        if body.get(src) is not None:
            req[dst] = body[src]
    if body.get("stream"):
        req["stream"] = True
        req["stream_options"] = {"include_usage": True}
    return req


def tool_input(arguments) -> dict:
    """A tool call's arguments as an object. A model that sends two JSON objects back to back gets
    the first one; text that is no object at all gives {}."""
    if isinstance(arguments, dict):
        return arguments
    text = str(arguments or "").strip()
    if not text:
        return {}
    try:
        got, _ = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def usage_of(u: dict | None) -> dict:
    u = u or {}
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    return {"input_tokens": max(int(u.get("prompt_tokens") or 0) - cached, 0),
            "output_tokens": int(u.get("completion_tokens") or 0),
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}


def _msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def from_chat(resp: dict, model: str) -> dict:
    """The Messages response for a non-streamed Chat Completions response (reasoning dropped)."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        content.append({"type": "tool_use", "id": c.get("id") or "toolu_" + uuid.uuid4().hex[:24],
                        "name": fn.get("name"), "input": tool_input(fn.get("arguments"))})
    stop = "tool_use" if msg.get("tool_calls") else STOP_REASONS.get(choice.get("finish_reason") or "stop", "end_turn")
    return {"id": _msg_id(), "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": usage_of(resp.get("usage"))}


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Stream:
    """Turns Chat Completions stream chunks into Messages SSE events. Text streams as it comes;
    tool calls are gathered by their index (ids matched, arguments joined, interleaved calls kept
    apart) and sent whole, in index order, when the upstream stream ends."""

    def __init__(self, model: str):
        self.model, self.index, self.text_open, self.started = model, 0, False, False
        self.calls: dict[int, dict] = {}
        self.finish, self.usage = "", {}

    def start(self) -> bytes:
        self.started = True
        return _sse("message_start", {"type": "message_start", "message": {
            "id": _msg_id(), "type": "message", "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": usage_of(None)}})

    def feed(self, chunk: dict) -> bytes:
        out = b"" if self.started else self.start()
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                if not self.text_open:
                    out += _sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                        "content_block": {"type": "text", "text": ""}})
                    self.text_open = True
                out += _sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                    "delta": {"type": "text_delta", "text": text}})
            for c in delta.get("tool_calls") or []:
                i = int(c.get("index") or 0)
                call = self.calls.setdefault(i, {"id": "", "name": "", "args": ""})
                fn = c.get("function") or {}
                call["id"] = call["id"] or c.get("id") or ""
                call["name"] = call["name"] or fn.get("name") or ""
                call["args"] += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
        return out

    def _close_text(self) -> bytes:
        if not self.text_open:
            return b""
        self.text_open = False
        self.index += 1
        return _sse("content_block_stop", {"type": "content_block_stop", "index": self.index - 1})

    def end(self) -> bytes:
        out = (b"" if self.started else self.start()) + self._close_text()
        for i in sorted(self.calls):
            c = self.calls[i]
            out += _sse("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": {
                "type": "tool_use", "id": c["id"] or "toolu_" + uuid.uuid4().hex[:24], "name": c["name"], "input": {}}})
            out += _sse("content_block_delta", {"type": "content_block_delta", "index": self.index, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(tool_input(c["args"]))}})
            out += _sse("content_block_stop", {"type": "content_block_stop", "index": self.index})
            self.index += 1
        stop = "tool_use" if self.calls else STOP_REASONS.get(self.finish or "stop", "end_turn")
        out += _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                      "usage": usage_of(self.usage)})
        return out + _sse("message_stop", {"type": "message_stop"})


def too_long(detail: str) -> str:
    """Anthropic's wording for an upstream prompt-too-long answer ('prompt is too long: N tokens > M
    maximum', the numbers when the answer gives them), which Claude Code compacts on; "" for another."""
    if not CONTEXT_RE.search(detail or ""):
        return ""
    limit = re.search(r"(?:maximum context length|context (?:length|window|size)|must be <=)\D{0,20}?(\d{3,})", detail, re.I)
    used = re.search(r"(?:requested|resulted in|contains|given:?|prompt has|you (?:sent|have))\D{0,20}?(\d{3,})",
                     detail, re.I)
    if limit and used and int(used.group(1)) > int(limit.group(1)):
        return f"prompt is too long: {used.group(1)} tokens > {limit.group(1)} maximum"
    return "prompt is too long"


def error_for(status: int, detail: str = "") -> tuple[int, dict]:
    """The Messages error (status, body) for an upstream HTTP status. The wording carries what the
    provider's classifiers match on: 'invalid api key' (auth gate), 'insufficient credits' (funds),
    'rate limit' (limit) and 'overloaded'; other 5xx stay retryable api_errors. A prompt past the
    context window is Anthropic's 'prompt is too long', which Claude Code compacts on."""
    long = too_long(detail) if status in CONTEXT_STATUSES else ""
    detail = (detail or "").strip()[:300]
    if long:
        return 400, {"type": "error", "error": {"type": "invalid_request_error", "message":
                     f"{long} (the upstream model's context window, HTTP {status})" + (f": {detail}" if detail else "")}}
    if status in (401, 403):
        kind, code, msg = "authentication_error", 401, "invalid api key: the upstream endpoint refused it"
    elif status == 402:
        # As a 429: Claude Code reads a 403 as a failed login, which would hold the provider as logged out.
        kind, code, msg = "rate_limit_error", 429, "insufficient credits on the upstream endpoint"
    elif status == 429:
        kind, code, msg = "rate_limit_error", 429, "rate limit reached on the upstream endpoint"
    elif status in (503, 529):
        kind, code, msg = "overloaded_error", 529, "the upstream endpoint is overloaded"
    elif status >= 500 or status == 0:
        kind, code, msg = "api_error", 500 if status == 0 else status, "the upstream endpoint failed"
    elif status == 404:
        kind, code, msg = "not_found_error", 404, "the upstream endpoint or model was not found"
    else:
        kind, code, msg = "invalid_request_error", 400, "the upstream endpoint rejected the request"
    full = f"{msg} (HTTP {status or 'no answer'})" + (f": {detail}" if detail else "")
    return code, {"type": "error", "error": {"type": kind, "message": full}}


RATE_WORDS = re.compile(r"rate.?limit|too many requests", re.I)
# A 404's wording that names the model as the missing thing (OpenAI, vLLM, LiteLLM, TGI and the like).
MODEL_GONE_WORDS = re.compile(
    r"model\b[^.\n]{0,200}?\b(?:not found|does ?n[o']t exist|not exist|is not (?:available|served|supported)"
    r"|not available|unavailable|no longer|deprecated|retired|decommissioned)"
    r"|\b(?:unknown|invalid|no such|unsupported) model|model_not_found", re.I)


def fault_of(status: int, detail: str = "") -> str:
    """The endpoint fault an upstream answer is, for per-tier routing's fallback (route.py): down (no
    answer or timed out), model_missing (a 404 whose wording names the model), error (5xx, any other
    404), rate_limit (429, 402 or rate-limit wording) or auth (401/403); '' for an answer about the
    request itself."""
    if status in (401, 403):
        return "auth"
    if status in (402, 429) or RATE_WORDS.search(detail or ""):
        return "rate_limit"
    if status == 0:
        return "down"
    if status == 404 and MODEL_GONE_WORDS.search(detail or ""):
        return "model_missing"
    return "error" if status >= 500 or status == 404 else ""


CODE_RE = re.compile(r"[A-Za-z][A-Za-z_-]{1,47}")   # an error code word: no digits, dots, slashes or colons


def upstream_of(status: int, body, key: str = "") -> dict:
    """What a failed upstream answer was, safe for the run's files: {"status": its HTTP status, "code":
    its error code (model_not_found), else its error type} when it gave one. The code is kept only as a
    plain word (CODE_RE: no digits, so no key or token; no dots, slashes or colons, so no URL or host)
    that no stored secret or the key is in; never the message. {} for no answer (status 0)."""
    if not status:
        return {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = None
    err = body.get("error") if isinstance(body, dict) else None
    code = ""
    for c in ((err.get("code"), err.get("type")) if isinstance(err, dict) else ()):
        if isinstance(c, str) and CODE_RE.fullmatch(c) and (not key or key not in c) and redact.scrub(c) == c:
            code = c
            break
    return {"status": int(status), **({"code": code} if code else {})}


def estimate_tokens(body: dict) -> int:
    """count_tokens: about 4 characters a token over the whole request (the endpoint has no counter)."""
    return max(1, len(json.dumps({k: body.get(k) for k in ("system", "messages", "tools")})) // 4)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ttp-chatbridge"
    bridge: "Bridge"

    def log_message(self, *args) -> None:   # nothing logged: paths and headers stay out of any file
        pass

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        got = self.headers.get("x-api-key") or ""
        bearer = self.headers.get("Authorization") or ""
        if bearer.lower().startswith("bearer "):
            got = got or bearer[7:]
        return hmac.compare_digest(got.strip().encode(), self.bridge.token.encode())

    def do_GET(self) -> None:
        self._reply(404, {"type": "error", "error": {"type": "not_found_error", "message": "not found"}})

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._reply(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                "message": "request body is not JSON"}})
        if not self._authorized():
            return self._reply(401, {"type": "error", "error": {
                "type": "authentication_error", "message": "invalid api key for the local bridge"}})
        if path == "/v1/messages/count_tokens":
            return self._reply(200, {"input_tokens": estimate_tokens(body)})
        if path != "/v1/messages":
            return self.do_GET()
        self.bridge.forward(self, body)


class Bridge:
    """One run's bridge: start() binds 127.0.0.1:<ephemeral> and serves in a daemon thread; stop()
    closes the port. Use as a context manager."""

    def __init__(self, base_url: str, key: str, model: str, max_tokens: int = 0,
                 timeout: float = UPSTREAM_TIMEOUT_S, context_window: int = 0, effort: str = "",
                 fields: dict | None = None, standins: list | None = None, served: list | None = None):
        self.base_url, self._key, self.model = endpoint.base_url(base_url), key, model
        self.max_tokens, self.timeout = int(max_tokens or 0), timeout
        self.context_window = int(context_window or 0)
        self.effort, self.fields, self.rejected = effort, dict(fields or {}), False   # effort_fields
        self.token = secrets.token_urlsafe(24)
        self.last_fault, self.faults = "", {}   # the last call's endpoint fault kind (fault_of), counts
        self.upstream: dict = {}   # the last failed answer's HTTP status and error code (upstream_of)
        self._listed: bool | None | str = ""    # _model_listed's answer, "" until asked
        self.standins = [s for s in standins or () if isinstance(s, dict) and s.get("model")]   # ranked, best first
        self.served = [str(d) for d in served or ()]   # state dirs whose served list (route.MODELS_FILE) is read
        self.mid_run: list[dict] = []   # each swap: {from, to, at, why}
        self.on_swap = None   # called with {asked, ran} after each swap (the runner's live SWAP_FILE)
        self._errs, self._left, self._swap_lock = 0, set(), threading.Lock()   # 5xx/no-answer run; models left
        self.server: http.server.ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @classmethod
    def from_settings(cls, s: dict, **kw) -> "Bridge":
        """The bridge for a run.json `bridge` entry, its key read from the `ttp secret endpoint` store."""
        alias, url = s.get("endpoint") or "", s.get("base_url") or ""
        if not alias:
            raise BridgeError("providers.chat.endpoint is not set: save a key with "
                              "`ttp secret endpoint <alias> --url <base url>` and set it to that alias")
        got = endpoint.credentials(alias)
        if not got:
            raise BridgeAuthError(f"failed to authenticate: no key saved for endpoint {alias!r} "
                              f"(run `ttp secret endpoint {alias} --url <base url>`)")
        key, saved = got
        try:
            same = not url or endpoint.base_url(url) == endpoint.base_url(saved)
        except ValueError as e:
            raise BridgeError(f"providers.chat.base_url: {e}") from None
        if not same:
            raise BridgeAuthError(f"failed to authenticate: providers.chat.base_url is not the URL saved with "
                                  f"endpoint {alias!r}: the key goes only to the URL it was saved for")
        if not s.get("model"):
            raise BridgeError("no model for this tier: set providers.chat.tiers.<tier>.model")
        return cls(url or saved, key, s["model"], s.get("max_tokens") or 0,
                   **{"context_window": s.get("context_window") or 0, "effort": s.get("effort") or "",
                      "fields": s.get("fields") or {}, "standins": s.get("standins") or [],
                      "served": s.get("served") or [], **kw})

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}" if self.server else ""

    def env(self) -> dict[str, str]:
        """What the agent gets to reach the bridge: its URL and the per-run token, never the key."""
        return {"ANTHROPIC_BASE_URL": self.url, "ANTHROPIC_AUTH_TOKEN": self.token, "ANTHROPIC_API_KEY": "",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                # Claude Code takes 200k for a model it does not know: compact within the tier's window.
                **({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(self.context_window)} if self.context_window else {})}

    def start(self) -> "Bridge":
        handler = type("Handler", (_Handler,), {"bridge": self})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                                       name="chatbridge", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            if self.thread is not None:
                self.thread.join(timeout=5)

    def __enter__(self) -> "Bridge":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _scrub(self, text: str) -> str:
        return redact.scrub(text.replace(self._key, "<key>") if self._key else text)

    def _fault(self, status: int, detail: str = "") -> None:
        kind = fault_of(status, detail)
        if kind == "error" and status == 404 and self._model_listed() is False:
            kind = "model_missing"   # a bare 404, and the endpoint's /models leaves the model out
        self.last_fault = kind
        self.upstream = upstream_of(status, detail, self._key)
        with self._swap_lock:   # calls in parallel count their errors in a row, none lost
            self._errs = self._errs + 1 if status == 0 or status >= 500 else 0
        if kind:
            self.faults[kind] = self.faults.get(kind, 0) + 1

    def _model_listed(self) -> bool | None:
        """Whether GET <base>/models lists this run's model (asked once per bridge), or None when the
        answer lists none or the check failed."""
        if self._listed == "":
            ok, _, ids = endpoint.list_models(self.base_url, self._key, timeout=MODEL_CHECK_TIMEOUT_S)
            self._listed = (self.model in ids) if ok and ids else None
        return self._listed

    def _fault_reply(self, reply: tuple[int, dict], body: dict | None = None) -> tuple[int, dict]:
        code, err = reply
        self._fault(code, str((err.get("error") or {}).get("message") or ""))
        if body is not None:   # an error in a 200 answer or a stream chunk: its own status when it gives one
            e = body.get("error") if isinstance(body.get("error"), dict) else {}
            given = str(e.get("status") or e.get("code") or "")
            self.upstream = upstream_of(int(given) if given.isdigit() and 400 <= int(given) < 600 else 200,
                                        body, self._key)
        return code, err

    def _malformed(self, detail: str) -> tuple[int, dict]:
        self.last_fault, self.upstream = "malformed", {}   # an endpoint fault for routing (route.py), not the run's
        self.faults["malformed"] = self.faults.get("malformed", 0) + 1
        return error_for(502, detail)

    def _ok(self) -> None:
        with self._swap_lock:
            self.last_fault, self.upstream, self._errs = "", {}, 0

    def _served_ids(self) -> list[str] | None:
        """The ids the served lists name now (modelrank.served, each fresh), or None when none is known."""
        from pathlib import Path
        from . import modelrank
        got, now = None, time.time()
        for d in self.served:
            ids = modelrank.served(Path(d), now)
            if ids is not None:
                got = (got or []) + ids
        return got

    def _swap_why(self, used: str) -> str:
        """Why the last answer for `used` swaps it: 'refused', 'not listed' (SWAP_ERRORS errors in a row
        and the served lists leave it out), else ''."""
        if not self.standins:
            return ""
        if self.last_fault == "model_missing" or self.upstream.get("code") in REFUSAL_CODES:
            return "refused"
        if self._errs >= SWAP_ERRORS and (ids := self._served_ids()) is not None and used not in ids:
            return "not listed"
        return ""

    def _swapped(self, used: str) -> bool:
        """True when the next try of this call goes to another model: this answer swapped `used` for the
        first served stand-in the run has not left, or another call already did."""
        why = self._swap_why(used)
        if not why:
            return False
        with self._swap_lock:
            if self.model != used:
                return True
            ids = self._served_ids() or []
            alt = next((s for s in self.standins if s["model"] not in self._left and s["model"] != used
                        and s["model"] in ids), None)
            if alt is None:
                return False   # no stand-in served: the error goes back as before
            self._left.add(used)
            self.model, self.fields, self.rejected = alt["model"], dict(alt.get("fields") or {}), False
            self.context_window = int(alt.get("context_window") or 0)
            self._listed, self._errs, self.last_fault, self.upstream = "", 0, "", {}
            self.mid_run.append({"from": used, "to": alt["model"], "at": round(time.time(), 3), "why": why})
            now = {"asked": self.mid_run[0]["from"], "ran": self.model}
        if self.on_swap:
            try:
                self.on_swap(now)
            except Exception:   # a live display hint, never the run's fault
                pass
        return True

    def stats(self) -> dict:
        """For the run's exit record: fault kinds, and the last failed answer's HTTP status and error
        code (`upstream`, upstream_of), never upstream text (it may echo a key); the effort fields sent
        and whether the endpoint rejected them, when the run had any."""
        return {"last_fault": self.last_fault, "faults": dict(self.faults),
                **({"upstream": dict(self.upstream)} if self.upstream else {}),
                **({"effort": self.effort, "fields": dict(self.fields), "rejected": self.rejected}
                   if self.fields else {}),
                **({"mid_run": [dict(m) for m in self.mid_run], "model": self.model} if self.mid_run else {})}

    def _open(self, req: dict):
        r = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(req).encode(), headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
            "Accept": "text/event-stream" if req.get("stream") else "application/json"})
        return urllib.request.build_opener(endpoint._NoRedirect).open(r, timeout=self.timeout)

    def forward(self, h: _Handler, body: dict) -> None:
        while True:
            with self._swap_lock:   # one model and its own fields: a swap in another call changes all three
                used, fields, rejected = self.model, dict(self.fields), self.rejected
            if self._errs >= SWAP_ERRORS and self._swapped(used):   # e.g. a stream that broke 3 times
                continue
            req = to_chat(body, used, self.max_tokens)
            try:
                try:
                    resp = self._open(with_fields(req, {} if rejected else fields))
                except urllib.error.HTTPError as e:
                    if rejected or not fields:
                        raise
                    try:
                        detail = e.read(2000).decode("utf-8", "replace")
                    except Exception:
                        detail = ""
                    if not rejects(e.code, detail, fields):
                        e.ttp_detail = detail   # read once: the handler below reports it
                        raise
                    with self._swap_lock:   # once, then the rest of the run goes without them (`used`'s only)
                        if self.model == used:
                            self.rejected = True
                    resp = self._open(req)
            except urllib.error.HTTPError as e:
                detail = getattr(e, "ttp_detail", None)
                if detail is None:
                    try:
                        detail = e.read(2000).decode("utf-8", "replace")
                    except Exception:
                        detail = ""
                self._fault(e.code, detail)
                if self._swapped(used):
                    continue
                return h._reply(*error_for(e.code, self._scrub(detail)))
            except (urllib.error.URLError, OSError) as e:
                self._fault(0)
                if self._swapped(used):
                    continue
                return h._reply(*error_for(0, self._scrub(str(getattr(e, "reason", e)))))
            break
        with resp:
            if not req.get("stream"):
                try:
                    data = json.loads(resp.read())
                except ValueError:
                    return h._reply(*self._malformed("the upstream answer is not JSON"))
                if data.get("error"):
                    return h._reply(*self._fault_reply(_chunk_error(data, self._scrub), data))
                self._ok()
                return h._reply(200, from_chat(data, body.get("model") or self.model))
            self._stream(h, resp, Stream(body.get("model") or self.model))

    def _stream(self, h: _Handler, resp, st: Stream) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()

        def send(data: bytes) -> None:
            if data:
                h.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                h.wfile.flush()

        errored = False
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                if not payload:
                    continue
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    chunk = None
                if not isinstance(chunk, dict):
                    # Fails the answer, even after good chunks: the lost piece may hold a tool call's
                    # arguments, and a retry beats a silently wrong call. No upstream text: it may echo the key.
                    send(_sse("error", self._malformed("the upstream stream sent a chunk that is not a JSON object")[1]))
                    errored = True
                    break
                if chunk.get("error"):
                    code, err = self._fault_reply(_chunk_error(chunk, self._scrub), chunk)
                    send(_sse("error", err))
                    errored = True
                    break
                send(st.feed(chunk))
            if not errored:
                self._ok()
                send(st.end())
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()
        except (OSError, socket.timeout) as e:
            self._fault(0)
            try:   # the upstream stream broke: say so in the stream, as the API does
                send(_sse("error", error_for(0, self._scrub(str(e)))[1]))
                h.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass
            h.close_connection = True


def _chunk_error(data: dict, scrub) -> tuple[int, dict]:
    """An error the upstream sent in a 200 body or a stream chunk: its own code when it gives one."""
    err = data.get("error") or {}
    if not isinstance(err, dict):
        err = {"message": str(err)}
    try:
        code, given = int(err.get("code") or err.get("status") or 500), True
    except (TypeError, ValueError):
        code, given = 500, False
    given = given and bool(err.get("code") or err.get("status"))
    message = scrub(str(err.get("message") or ""))
    # A string code (context_length_exceeded) is not a status: it still names a prompt past the window.
    # A status the answer gives (a 429 for tokens per minute) keeps its own meaning.
    hint = " ".join(str(err[k]) for k in ("code", "type") if err.get(k) and not str(err[k]).isdigit())
    if (not given or code in CONTEXT_STATUSES) and too_long(f"{hint} {message}"):
        code, message = 400, f"{hint}: {message}" if hint else message
    return error_for(code if 400 <= code < 600 else 500, message)
