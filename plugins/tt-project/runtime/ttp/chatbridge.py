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
overloaded and other 5xx as a retryable api_error.
"""
from __future__ import annotations

import hmac
import http.server
import json
import secrets
import socket
import threading
import urllib.error
import urllib.request
import uuid

from . import endpoint

UPSTREAM_TIMEOUT_S = 600
STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                "function_call": "tool_use", "content_filter": "end_turn"}


class BridgeError(Exception):
    """The bridge cannot start: no key saved, or the config names another URL than the key's."""
    auth = False


class BridgeAuthError(BridgeError):
    """No usable key for the endpoint: the provider's auth gate, not a launch failure (the runner
    records it as `launch_auth`, the daemon books the run `auth`)."""
    auth = True


def settings(cfg: dict, tier: str, model: str) -> dict:
    """The run.json `bridge` entry for a `chat` run: no key, only where to find it."""
    entry = ((cfg or {}).get("providers") or {}).get("chat") or {}
    cap = ((entry.get("tiers") or {}).get(tier) or {}).get("max_tokens") or entry.get("max_tokens") or 0
    return {"endpoint": str(entry.get("endpoint") or ""), "base_url": str(entry.get("base_url") or ""),
            "model": model, "max_tokens": int(cap or 0)}


def config_problems(raw: dict) -> list[str]:
    """What is wrong with project.json's providers.chat entry (none when it has none)."""
    entry = (raw.get("providers") or {}).get("chat") if isinstance(raw.get("providers"), dict) else None
    if entry is None:
        return []
    if not isinstance(entry, dict):
        return ["providers.chat: not an object; chat runs cannot start"]
    out = []
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
    caps = [entry.get("max_tokens"), *((t or {}).get("max_tokens") for t in (entry.get("tiers") or {}).values()
                                       if isinstance(t, dict))]
    if any(c is not None and (isinstance(c, bool) or not isinstance(c, int) or c < 0) for c in caps):
        out.append("providers.chat max_tokens: give a whole number of tokens (0 = no cap)")
    return out


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


def error_for(status: int, detail: str = "") -> tuple[int, dict]:
    """The Messages error (status, body) for an upstream HTTP status. The wording carries what the
    provider's classifiers match on: 'invalid api key' (auth gate), 'insufficient credits' (funds),
    'rate limit' (limit) and 'overloaded'; other 5xx stay retryable api_errors."""
    detail = (detail or "").strip()[:300]
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
                 timeout: float = UPSTREAM_TIMEOUT_S):
        self.base_url, self._key, self.model = endpoint.base_url(base_url), key, model
        self.max_tokens, self.timeout = int(max_tokens or 0), timeout
        self.token = secrets.token_urlsafe(24)
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
        return cls(url or saved, key, s["model"], s.get("max_tokens") or 0, **kw)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}" if self.server else ""

    def env(self) -> dict[str, str]:
        """What the agent gets to reach the bridge: its URL and the per-run token, never the key."""
        return {"ANTHROPIC_BASE_URL": self.url, "ANTHROPIC_AUTH_TOKEN": self.token, "ANTHROPIC_API_KEY": "",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

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
        return text.replace(self._key, "<key>") if self._key else text

    def forward(self, h: _Handler, body: dict) -> None:
        req = to_chat(body, self.model, self.max_tokens)
        r = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(req).encode(), headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
            "Accept": "text/event-stream" if req.get("stream") else "application/json"})
        try:
            resp = urllib.request.build_opener(endpoint._NoRedirect).open(r, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read(2000).decode("utf-8", "replace")
            except Exception:
                detail = ""
            return h._reply(*error_for(e.code, self._scrub(detail)))
        except (urllib.error.URLError, OSError) as e:
            return h._reply(*error_for(0, self._scrub(str(getattr(e, "reason", e)))))
        with resp:
            if not req.get("stream"):
                try:
                    data = json.loads(resp.read())
                except ValueError:
                    return h._reply(*error_for(502, "the upstream answer is not JSON"))
                if data.get("error"):
                    return h._reply(*_chunk_error(data, self._scrub))
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
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("error"):
                    code, err = _chunk_error(chunk, self._scrub)
                    send(_sse("error", err))
                    errored = True
                    break
                send(st.feed(chunk))
            if not errored:
                send(st.end())
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()
        except (OSError, socket.timeout) as e:
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
        code = int(err.get("code") or err.get("status") or 500)
    except (TypeError, ValueError):
        code = 500
    return error_for(code if 400 <= code < 600 else 500, scrub(str(err.get("message") or "")))
