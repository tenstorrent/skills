"""A bench arm's Claude models behind the run's loopback bridge, for an OpenAI-tool-calling agent
(mini-swe-agent): each /v1/chat/completions call runs the logged-in `claude` CLI once in print mode.

The CLI is a bare model endpoint here: the agent's own system prompt replaces Claude Code's
(--system-prompt, never appended), every built-in tool, skill, plugin, MCP server, hook, settings source,
auto memory and CLAUDE.md is off, and it runs from an empty scratch folder. The agent's tool (its bash
loop) is the only tool: its schema is written into the user turn, the conversation so far is rendered
after it, and the reply's tool-call blocks become the OpenAI `tool_calls` the agent expects. The CLI keeps
its existing login (never copied, never printed) and never uses an API key. Its JSON result carries the
call's usage and cost, which the bridge sums for the cell (`usage`, with `cost_usd`)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import chatbridge
from .chatbridge import Bridge, BridgeError

CALL_TIMEOUT_S = 900.0   # one model call (the cell's own limit stops a stuck run)
RETRIES = 3   # a call the CLI answered with a transient error (overload, rate limit) is tried again
RETRY_WAIT_S = 20.0
ASSISTANT_MARK = "\n\n=== assistant ==="
# Environment the CLI must not inherit: any variable of an enclosing Claude Code session (its session,
# entrypoint or socket) or another login (a token, a base URL, a cloud switch) changes what it loads or where
# it goes. The login itself is the CLI's own store under HOME, never an environment variable.
DROP_PREFIXES = ("CLAUDE", "ANTHROPIC")
FIXED_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
             "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
             "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1"}


def cli_argv(model: str, effort: str, system_prompt: str) -> list[str]:
    """The claude CLI's argv for one call: print mode, the agent's system prompt in place of Claude Code's,
    no tools, no settings sources (so no user, project or local settings, hooks, plugins or memory), no
    skills or slash commands, no MCP servers, no saved session. The user turn comes on stdin as one
    stream-json message of text blocks (one per conversation message: the prompt cache keeps each turn's
    prefix, which a single text block never reuses)."""
    argv = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--model", model]
    if effort:
        argv += ["--effort", effort]
    return argv + ["--system-prompt", system_prompt, "--tools", "", "--setting-sources", "",
                   "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
                   "--exclude-dynamic-system-prompt-sections"]


def stdin_message(blocks: list[str]) -> str:
    """The one stream-json user message the CLI reads: the blocks as text blocks of a single turn."""
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": b} for b in blocks]}}) + "\n"


def result_event(stdout: str) -> dict | None:
    """The `result` event of a stream-json run (the last one), or None."""
    res = None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            res = ev
    return res


def cli_env(env: dict | None = None) -> dict[str, str]:
    """The CLI's environment: the caller's, less every CLAUDE* and ANTHROPIC* variable, plus FIXED_ENV."""
    base = {k: v for k, v in (os.environ if env is None else env).items() if not k.startswith(DROP_PREFIXES)}
    return {**base, **FIXED_ENV}


def _text(content) -> str:
    return chatbridge._text(content) if content is not None else ""


def tool_header(tools: list[dict]) -> str:
    """The user turn's opening: the agent's tools and how to call one. A tool with a single string
    parameter is called with that value as the block's raw body (no JSON escaping of a shell command)."""
    lines = ["You can call tools. To call one, write a block with its name and the argument(s) as shown, "
             "after any text. Write one block per reply and stop right after it: its result comes back in the next "
             "turn. Further blocks in the same reply are discarded.", ""]
    for t in tools or ():
        fn = t.get("function") or {}
        props = (fn.get("parameters") or {}).get("properties") or {}
        single = _single_string(fn)
        lines.append(f"Tool `{fn.get('name')}`: {fn.get('description') or ''}".rstrip())
        for pname, p in props.items():
            lines.append(f"  parameter `{pname}` ({(p or {}).get('type') or 'string'}): "
                         f"{(p or {}).get('description') or ''}".rstrip())
        if single:
            lines += ["  call it as (the text between the tags is the whole value, written as is):",
                      f'<tool_call name="{fn.get("name")}">', f"the {single} exactly as you would type it", "</tool_call>"]
        else:
            lines += ["  call it as:", f'<tool_call name="{fn.get("name")}">', '{"parameter": "value", ...}', "</tool_call>"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _single_string(fn: dict) -> str:
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    if len(props) == 1:
        (name, p), = props.items()
        if (p or {}).get("type", "string") == "string":
            return name
    return ""


def render(messages: list[dict], tools: list[dict]) -> tuple[str, list[str]]:
    """(system prompt, user-turn blocks) for a chat-completions call: the system messages joined, byte for
    byte; the blocks are the tool header, then each message in order (consecutive tool results are one
    block). A block that is not an assistant turn ends on the assistant marker when an assistant turn
    follows it or it is last, so no later message changes an earlier block: each call's blocks start with the
    previous call's, and the prompt cache reuses them. No block ends in whitespace (the CLI trims the last
    block's tail, which would change it once more blocks follow); the separator leads the next block."""
    system, turns = [], []   # turns: [role, text]
    singles = {(t.get("function") or {}).get("name"): _single_string(t.get("function") or {}) for t in tools or ()}
    for m in messages:
        role, body = m.get("role"), _text(m.get("content"))
        if role in ("system", "developer"):
            system.append(body)
        elif role == "user":
            turns.append(["user", f"=== user ===\n{body}\n\n"])
        elif role == "assistant":
            calls = []
            for c in m.get("tool_calls") or ():
                fn = c.get("function") or {}
                name, args = fn.get("name"), chatbridge.tool_input(fn.get("arguments"))
                single = singles.get(name)
                inner = str(args.get(single, "")) if single and single in args else json.dumps(args)
                calls.append(f'<tool_call name="{name}" id="{c.get("id") or ""}">\n{inner}\n</tool_call>')
            turns.append(["assistant", "\n".join(x for x in [body, *calls] if x) + "\n\n"])
        elif role == "tool":
            text = f'=== tool result (id {m.get("tool_call_id") or ""}) ===\n{body}\n\n'
            if turns and turns[-1][0] == "tool":
                turns[-1][1] += text
            else:
                turns.append(["tool", text])
    blocks = [tool_header(tools).rstrip()] if tools else []
    for i, (role, text) in enumerate(turns):
        last = i == len(turns) - 1
        text = text.rstrip()
        if role != "assistant" and (last or turns[i + 1][0] == "assistant"):
            text += ASSISTANT_MARK
        blocks.append(("\n\n" if blocks else "") + text)
    if turns and turns[-1][0] == "assistant":
        blocks.append(ASSISTANT_MARK)   # the model goes on in its own turn
    return "\n\n".join(system), blocks


def _unwrap(body: str, single: str) -> str:
    """The value inside the tag wrappers a model puts around a lone string argument: <command>x</command>,
    <parameter name="command">x</parameter>, <parameter>x</parameter> (nested, or with a stray closer)."""
    name = re.escape(single)
    closer = rf"</(?:{name}|parameter)>"
    m = re.match(rf'\s*<(?:{name}|parameter(?:\s+name="{name}")?)>', body)
    while m:   # a wrapper opened: drop it and its closers; a bare value is left as it is
        body = re.sub(rf"\s*(?:{closer}\s*)+$", "", body[m.end():])
        m = re.match(rf'\s*<(?:{name}|parameter(?:\s+name="{name}")?)>', body)
    return body


_BLOCK = re.compile(r'<tool_call(?P<attrs>[^>]*)>\n?(?P<body>.*?)\n?</(?:tool_call|invoke)>', re.DOTALL)
_NAME = re.compile(r'name="([^"]*)"')
# The XML call form the CLI's models are trained on: <invoke name="x"><parameter name="p">v</parameter></invoke>
_INVOKE = re.compile(r'<invoke\s+name="(?P<name>[^"]*)"\s*>(?P<body>.*?)</(?:invoke|tool_call)>', re.DOTALL)
_PARAM = re.compile(r'<parameter\s+name="([^"]*)"\s*>(.*?)</parameter>', re.DOTALL)


def _typed(found: dict, schema: dict) -> dict:
    """The parameters read from XML tags, as strings, given the types their tool's schema declares."""
    props = schema.get("properties") or {}
    out = {}
    for k, v in found.items():
        kind = (props.get(k) or {}).get("type")
        try:
            out[k] = int(v) if kind == "integer" else float(v) if kind == "number" else \
                v.strip().lower() == "true" if kind == "boolean" else json.loads(v) if kind in ("array", "object") else v
        except ValueError:
            out[k] = v
    return out


_OPEN = re.compile(r'<tool_call\b[^>]*>')
_STRAY = re.compile(r'(?:\s*</(?:tool_call_args|parameter|invoke)>)+\s*$')


def _tidy(text: str, singles: dict) -> str:
    """The reply with a model's slips in the call form mended: a doubled opener, a <tool_call_args> tag around
    the JSON, doubled closers, and a final block left open when its JSON arguments are whole."""
    text = re.sub(r'(<tool_call\b[^>]*>)(?:\s*<tool_call\b[^>]*>)+', r'\1', text)
    text = re.sub(r'(<tool_call\b[^>]*>\s*)<tool_call_args[^>]*>', r'\1', text)
    text = re.sub(r'</tool_call_args>(\s*</tool_call>)', r'\1', text)
    text = re.sub(r'(</tool_call>)(?:\s*</tool_call>)+', r'\1', text)
    last = None
    for last in _OPEN.finditer(text):
        pass
    if last and "</tool_call>" not in text[last.end():] and "</invoke>" not in text[last.end():]:
        name = (_NAME.search(last.group(0)) or [None, ""])[1]
        if singles.get(name) == "":   # a JSON-argument tool: the arguments must be whole to take an open block
            body = _STRAY.sub("", text[last.end():]).strip()
            try:
                json.loads(body, strict=False)
                text = text[:last.end()] + "\n" + body + "\n</tool_call>"
            except ValueError:
                pass
    return text


def parse_reply(text: str, tools: list[dict]) -> tuple[str, list[dict]]:
    """(text outside the tool blocks, OpenAI tool_calls) of the CLI's reply. A block for an unknown tool
    is left as text for the agent to meet (its format error), never dropped."""
    singles = {(t.get("function") or {}).get("name"): _single_string(t.get("function") or {}) for t in tools or ()}
    schemas = {(t.get("function") or {}).get("name"): (t.get("function") or {}).get("parameters") or {} for t in tools or ()}
    calls, n = [], 0

    def take_invoke(m: re.Match) -> str:
        nonlocal n
        name = m.group("name")
        if name not in singles:
            return m.group(0)
        params = {k: v.strip("\n") for k, v in _PARAM.findall(m.group("body"))}
        single = singles[name]
        if single:
            if single not in params and len(params) != 1:
                return m.group(0)
            args = {single: params.get(single, next(iter(params.values()), ""))}
        else:
            args = params
        n += 1
        calls.append({"id": f"call_{n}", "type": "function",
                      "function": {"name": name, "arguments": json.dumps(args)}})
        return ""

    def take(m: re.Match) -> str:
        nonlocal n
        name = (_NAME.search(m.group("attrs")) or [None, ""])[1]
        if name not in singles:
            return m.group(0)
        body = m.group("body")
        single = singles[name]
        if single:   # a model that wraps the value in a <parameter> tag is read as it meant
            args = {single: _unwrap(body, single)}
        else:
            try:
                args = json.loads(_STRAY.sub("", body) if body.lstrip().startswith("{") else body, strict=False)   # raw newlines in a string are read as meant
            except ValueError:   # the XML form a CLI model falls back to: <parameter name="p">v</parameter>
                found = {k: v.strip("\n") for k, v in _PARAM.findall(body)}
                if not found:
                    return m.group(0)
                args = _typed(found, schemas.get(name) or {})
        n += 1
        calls.append({"id": f"call_{n}", "type": "function",
                      "function": {"name": name, "arguments": json.dumps(args)}})
        return ""
    rest = _BLOCK.sub(take, _tidy(text.strip(), singles)).strip()
    rest = _INVOKE.sub(take_invoke, rest).strip()   # the XML form the model was trained on, alone or mixed in
    return rest, calls


def stream_chunks(answer: dict) -> list[dict]:
    """The Chat Completions stream chunks of one whole `answer`: the role, the text, the tool call (its
    arguments whole), the finish reason, then the usage."""
    base = {"id": answer["id"], "object": "chat.completion.chunk", "created": answer["created"], "model": answer["model"]}
    choice = answer["choices"][0]
    msg = choice["message"]
    delta = [{"role": "assistant", "content": ""}]
    if msg.get("content"):
        delta.append({"content": msg["content"]})
    for i, c in enumerate(msg.get("tool_calls") or ()):
        delta.append({"tool_calls": [{"index": i, "id": c["id"], "type": "function", "function": c["function"]}]})
    chunks = [{**base, "choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in delta]
    chunks.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]})
    chunks.append({**base, "choices": [], "usage": answer["usage"]})
    return chunks


def reply_stream(h, answer: dict) -> None:
    """Send `answer` as a Server-Sent Events stream (chunked), the form a streaming agent asks for."""
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Transfer-Encoding", "chunked")
    h.end_headers()
    try:
        for chunk in [*stream_chunks(answer), "[DONE]"]:
            data = (f"data: {chunk if isinstance(chunk, str) else json.dumps(chunk)}\n\n").encode()
            h.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        h.wfile.write(b"0\r\n\r\n")
        h.wfile.flush()
    except OSError:
        pass


def result_usage(res: dict) -> dict:
    """A CLI JSON result's usage as the bridge counts it: input without the cached share, output, cache
    reads and writes, and the CLI's own cost in USD."""
    u = res.get("usage") or {}
    return {"input_tokens": int(u.get("input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0),
            "cache_read_input_tokens": int(u.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(u.get("cache_creation_input_tokens") or 0),
            "cost_usd": float(res.get("total_cost_usd") or 0.0)}


def _kind(res: dict, rc: int, err: str) -> str:
    """The fault kind of a failed call: auth, capacity (rate limit, overload: worth another try) or error."""
    blob = f"{res.get('result') or ''} {err}".lower()
    if any(w in blob for w in ("not logged in", "/login", "authentication", "invalid api key", "oauth")):
        return "auth"
    if any(w in blob for w in ("rate limit", "usage limit", "overloaded", "529", "429", "try again", "timed out")):
        return "capacity"
    return "error"


class ClaudeCliBridge(Bridge):
    """The run's bridge with the logged-in claude CLI as its model: same surface as the chat bridge
    (url, openai_env, usage, stats), but no endpoint and no key."""

    def __init__(self, model: str, effort: str = "", timeout: float = CALL_TIMEOUT_S, retries: int = RETRIES,
                 retry_wait_s: float = RETRY_WAIT_S):
        super().__init__("http://127.0.0.1", "", model, effort=effort, timeout=timeout)
        self.retries, self.retry_wait_s = int(retries), float(retry_wait_s)
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_write_tokens": 0, "cost_usd": 0.0, "dropped_tool_calls": 0}
        self.cli_version = ""
        self.scratch = ""   # the empty working folder of every call
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, s: dict, **kw) -> "ClaudeCliBridge":
        if not shutil.which("claude"):
            raise BridgeError("the claude CLI is not on PATH")
        if not s.get("model"):
            raise BridgeError("no model for the claude arm")
        return cls(s["model"], s.get("effort") or "", **kw)

    def start(self) -> "ClaudeCliBridge":
        self.scratch = tempfile.mkdtemp(prefix="ttp-claude-bridge-")   # empty, and no CLAUDE.md above /tmp
        try:
            self.cli_version = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                                              timeout=60, cwd=self.scratch, env=cli_env()).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        super().start()
        return self

    def stop(self) -> None:
        super().stop()
        if self.scratch:
            shutil.rmtree(self.scratch, ignore_errors=True)

    def stats(self) -> dict:
        out = super().stats()
        out["claude_cli"] = {"version": self.cli_version, "model": self.model, "effort": self.effort,
                             "argv": cli_argv(self.model, self.effort, "<the agent's system prompt>")}
        return out

    def cost_so_far(self) -> float:
        """Dollars the CLI has reported so far (the runner's per-cell budget check)."""
        with self._lock:
            return float(self.usage.get("cost_usd") or 0.0)

    def call(self, system: str, blocks: list[str]) -> tuple[dict | None, str, str]:
        """One CLI run: (its result event or None, fault kind or "", stderr tail)."""
        try:
            p = subprocess.run(cli_argv(self.model, self.effort, system), input=stdin_message(blocks), capture_output=True,
                               text=True, timeout=self.timeout, cwd=self.scratch, env=cli_env())
        except subprocess.TimeoutExpired:
            return None, "capacity", "the claude CLI call timed out"
        except OSError as e:
            return None, "error", f"claude CLI did not start: {type(e).__name__}"
        res = result_event(p.stdout)
        err = (p.stderr or "")[-300:]
        if isinstance(res, dict) and not res.get("is_error") and p.returncode == 0:
            return res, "", err
        return res, _kind(res or {}, p.returncode, err or p.stdout[-300:]), err

    def relay(self, h, body: dict) -> None:
        tools = body.get("tools") or []
        system, blocks = render(body.get("messages") or [], tools)
        res, kind, err = None, "error", ""
        for attempt in range(self.retries + 1):
            timed = self.calls.begin()
            res, kind, err = self.call(system, blocks)
            if not kind:
                # the CLI's own API time is the call's generation time (its start-up is ours, not the model's)
                timed.end(result_usage(res)["output_tokens"], gen_s=float((res or {}).get("duration_api_ms") or 0) / 1000)
                break
            timed.end(0, "fault")
            with self._lock:
                self.last_fault = kind
                self.faults[kind] = self.faults.get(kind, 0) + 1
                if res:   # a failed call can still have cost something
                    for k, v in result_usage(res).items():
                        self.usage[k] += v
            if kind != "capacity" or attempt == self.retries:
                if kind == "capacity":
                    with self._lock:
                        self.busy["gave_up"] += 1   # a capacity stop, as the chat bridge counts one
                break
            wait = self.retry_wait_s * (attempt + 1)
            with self._lock:
                self.busy["waits"] += 1
                self.busy["waited_s"] += wait
            self.calls.sleep_wait(wait)
            time.sleep(wait)
        if kind:
            code = {"auth": 401, "capacity": 429}.get(kind, 502)
            msg = f"claude CLI {kind} fault" + (f": {(res or {}).get('result') or err}"[:300] if kind != "auth" else "")
            return h._reply(code, {"error": {"message": msg, "type": "api_error", "code": kind}})
        text, calls = parse_reply(str(res.get("result") or ""), tools)
        # The CLI cannot stop at a command (no tools), so a long reply may hold many; the model never saw
        # their output. Keep the first and drop the rest, as a native tool call ends the turn at the first.
        dropped = max(0, len(calls) - 1)
        calls = calls[:1]
        if dropped:
            print(f"claude bridge: kept the first command of the reply, dropped {dropped} more",
                  file=sys.stderr, flush=True)
        got = result_usage(res)
        with self._lock:
            self.usage["calls"] += 1
            self.usage["dropped_tool_calls"] += dropped
            for k, v in got.items():
                self.usage[k] += v
            self.last_fault = ""
        msg = {"role": "assistant", "content": text or None}
        if calls:
            msg["tool_calls"] = calls
        total_in = got["input_tokens"] + got["cache_read_input_tokens"] + got["cache_write_tokens"]
        answer = {"id": f"chatcmpl-{int(time.time() * 1000)}", "object": "chat.completion",
                  "created": int(time.time()), "model": self.model,
                  **({"ttp_dropped_tool_calls": dropped} if dropped else {}),
                  "choices": [{"index": 0, "message": msg,
                               "finish_reason": "tool_calls" if calls else "stop"}],
                  "usage": {"prompt_tokens": total_in, "completion_tokens": got["output_tokens"],
                            "total_tokens": total_in + got["output_tokens"]}}
        if body.get("stream"):   # a streaming agent (Codex through its proxy) reads the answer as chunks
            return reply_stream(h, answer)
        h._reply(200, answer)
