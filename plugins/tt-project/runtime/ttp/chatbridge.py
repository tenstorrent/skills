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
tool_result blocks (parallel calls matched by id), stop reasons and usage. Reasoning is dropped
both ways unless preserved thinking is on (below). The client's own max_tokens is dropped too: only a cap
the user set (providers.chat max_tokens, or a tier's) is sent, else none; calls are bounded by time.
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

Preserved thinking (`providers.chat.preserve_thinking`, default off; TTP_CHAT_PRESERVE_THINKING=1 or 0
in the environment of the process that calls settings() turns it on or off for that run alone, such as
one bench or A/B run): a model whose chat template keeps past reasoning (GLM-5.x renders a turn sent
without it as an empty <think></think>) gets its own reasoning back. The upstream's reasoning
(`reasoning_content`, else `reasoning`) reaches Claude Code as a thinking block with a signature, which
Claude Code sends back with the turn; each assistant turn's thinking goes upstream again unchanged and
in order, in one field (`field`, default reasoning: an endpoint may refuse a turn carrying both), with
chat_template_kwargs clear_thinking=false so the template keeps it whatever its default. On the /v1
relay an assistant turn's reasoning, in either field, is moved to that field. Guards: no
reasoning goes upstream for a model whose id contains a `skip_models` word (default "flash", any case),
and `max_turns` (0 = all) keeps it on only the last N assistant turns; stats() counts the turns sent.

Vendor defaults (`providers.chat.vendor_defaults`, default off; true for every lever, or an object
of lever: true; TTP_CHAT_VENDOR_DEFAULTS=1, 0 or lever names separated by commas sets them for one run,
like TTP_CHAT_PRESERVE_THINKING): a model family's published settings (VENDOR, by a word its model id
contains; `providers.chat.vendor_values.<word>` replaces any of them). Levers: `sampling` (request fields
such as temperature and top_p), `effort_map` (the run's effort sent as the model's own reasoning_effort
level, so no effort falls to the endpoint's default, which for GLM-5.x is max), `context_window` (the
model's real window replaces the tier's compact window), `timeout` (the agent's API_TIMEOUT_MS, and the
bridge waits upstream as long) and `attribution_header` (CLAUDE_CODE_ATTRIBUTION_HEADER=0: a header that
changes each call defeats the server's prefix cache). effort_fields win over sampling and effort_map;
stats() lists the levers on (`vendor`).

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

import email.utils
import hashlib
import hmac
import http.server
import io
import json
import os
import random
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

from . import endpoint, inflight, redact, serverhealth
from .providers.base import challenged

UPSTREAM_TIMEOUT_S = 600
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)   # the client hung up mid-answer
MODEL_CHECK_TIMEOUT_S = 10   # the /models check a bare 404 gets (model_listed)
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
# A model at capacity (HTTP 429, or a 503 that says when to come back): the bridge waits and sends the call
# again, honoring Retry-After (its header, else a "retry after N seconds" in the body, else 2, 4, 8 ... s),
# at most BUSY_RETRIES times and BUSY_WAIT_S seconds in all per call (providers.chat.busy_retries and
# busy_wait_s), each wait at most BUSY_WAIT_MAX_S. Past that the 429 goes to the agent as a capacity stop.
# Each wait is at least BUSY_WAIT_MIN_S (never an immediate retry) and gets up to BUSY_JITTER more (jitter),
# so the runs a refusal hit together do not all come back at once.
BUSY_RETRIES, BUSY_WAIT_S, BUSY_WAIT_MAX_S, BUSY_WAIT_MIN_S, BUSY_JITTER = 4, 180.0, 60.0, 1.0, 0.25
RETRY_AFTER_RE = re.compile(r"retry\s+(?:after|in)\s+(\d+(?:\.\d+)?)\s*s", re.I)
NEVER_FITS_RE = re.compile(r"request too large|insufficient.{0,20}(?:credit|quota|balance)", re.I)   # waiting cannot help
# A 429 whose body says code quota_exceeded: the endpoint's token quota is used up until its resets_at
# (an hourly window). Waiting inside the run cannot help: the runner ends the run for a provider fallback.
QUOTA_RE = re.compile(r'"code"\s*:\s*"quota_exceeded"')
RESETS_RE = re.compile(r'"resets_at"\s*:\s*"([^"\s]+)"')   # read with a pattern: a cut-off body keeps it


def quota_until(detail: str, now: float | None = None) -> float | None:
    """When the token quota a 429 body names (QUOTA_RE) resets, in epoch seconds: its resets_at, else
    the next full hour; None for any other body."""
    if not QUOTA_RE.search(detail or ""):
        return None
    now = time.time() if now is None else now
    try:
        return datetime.fromisoformat(RESETS_RE.search(detail).group(1).replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):   # no resets_at, or one that does not parse
        return now + 3600 - now % 3600


REASONING_FIELDS = ("reasoning", "reasoning_content")   # where Chat Completions servers put a turn's reasoning
# (vLLM 0.20 and later reads only `reasoning` on input, vllm issue 38488: the default field sent back)
THINKING_ENV = "TTP_CHAT_PRESERVE_THINKING"   # 1 or 0: preserved thinking on or off for one run (settings)
THINKING_SKIP = ("flash",)   # model id words whose runs never get reasoning back (preserve_thinking.skip_models)
# Bad output, counted per run (stats()["bad_output"]): tool calls Claude Code rejected for bad input (an
# is_error tool_result in a later request, worded as a <tool_use_error> input fault, each tool_use id once);
# tool calls whose arguments do not parse as an object (sent on as {}, which Claude Code then rejects);
# answers with no text and no tool call, only reasoning (empty_content); and answers whose reasoning, text
# or tool arguments match a junk pattern (providers.chat.junk_patterns, an object of name -> regex that
# replaces these; {} turns the junk scan off).
# Emission guard (providers.chat.emission_guard, default on; false: off): a tool call whose arguments do
# not parse is mended once, model-free, before Claude Code gets it (repair_args: a code fence around them,
# a trailing comma, closers cut off after a whole value; never a string, number or word cut short, which
# may be a cut-off command or value). One it cannot mend goes on as {}, which Claude Code rejects, so the
# model sends the call again.
# bad_output counts the mended ones (args_repaired) beside args_unparsed.
# Loop guard (providers.chat.loop_guard, default on; false: off): when the request's history ends with the
# same tool call failing LOOP_REPEATS times in a row (failing_streak: the same tool and input, each result
# is_error), the bridge adds one nudge to that request (LOOP_NUDGE). If that call then fails once more
# (the history sent again as it was, a retry or a side call, is not a new failure), the bridge answers
# that request with an error that ends the run and stats()["loop"] says why (`bounce`): on a GLM route the daemon restarts the task fresh on Opus, counted as a bounce, with
# no attempt spent (glmwork). stats()["loop"] counts nudges and stops for the run's meter.
LOOP_REPEATS = 3
LOOP_NUDGE = ("[harness] The same tool call has now failed {n} times in a row with the same input. Do not "
              "send it again: read the error, change the approach, or end with status needs_opus and say why.")
JUNK_PATTERNS = {"U+FFFD": "\ufffd", "AMEAME": "AMEAME", "paren_run": r"(?:\(\s){5,}",
                 "ById": r"(?<![A-Za-z0-9_.$])ById(?![A-Za-z0-9_])"}
TOOL_INPUT_RE = re.compile(
    r"InputValidationError|No such tool available|required parameter|unexpected parameter"
    r"|invalid (?:tool )?(?:parameters?|input|arguments?)|missing required (?:parameter|argument|field)"
    r"|could not be parsed|not valid JSON|invalid JSON|JSON\.?parse|unparseable|Unexpected token"
    r"|Unterminated string|Expected property name", re.I)
VENDOR_ENV = "TTP_CHAT_VENDOR_DEFAULTS"   # 1, 0 or lever names: the vendor defaults on or off for one run (settings)
VENDOR_LEVERS = ("sampling", "effort_map", "context_window", "timeout", "attribution_header")
# A model family's published settings, by a word its model id contains (any case); vendor_values overrides them.
# GLM-5.x (its vendor's docs): temperature 1.0, top_p 0.95; reasoning_effort low, high or max, max when none is
# sent; a 1,048,576-token window (measured: a 1,008,026-token prompt accepted); a 3,000,000 ms client timeout.
VENDOR = {"glm": {"sampling": {"temperature": 1.0, "top_p": 0.95},
                  "effort_map": {"low": "low", "medium": "high", "high": "high", "xhigh": "max", "max": "max",
                                 "*": "high"},
                  "context_window": 1_000_000, "timeout_ms": 3_000_000}}


class _ClientGone(Exception):
    """The client hung up while the bridge wrote to it: no upstream fault (an upstream read's OSError is one)."""


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
    busy = {k: entry[k] for k in ("busy_retries", "busy_wait_s") if _count_ok(entry.get(k))}
    cap = row.get("max_tokens") or entry.get("max_tokens") or 0
    levers = vendor_levers(entry)
    window = vendor_window(entry, model, levers) or window_of(entry, model, row)
    fields = run_fields(entry, model, effort, levers)
    think = thinking_of(entry)
    alts = [{"model": m, **({"fields": f} if (f := run_fields(entry, m, effort, levers)) else {}),
             **({"context_window": w} if (w := vendor_window(entry, m, levers) or window_of(entry, m)) else {})}
            for m in standins if m != model]
    client = vendor_client(entry, model, levers)
    return {"endpoint": str(entry.get("endpoint") or ""), "base_url": str(entry.get("base_url") or ""),
            "model": model, "max_tokens": int(cap or 0), **({"context_window": int(window)} if window else {}),
            **({"effort": effort, "fields": fields} if fields else {}),
            **({"effort": effort, "standins": alts, "served": [str(d) for d in served]} if alts and served else {}),
            **({"thinking": think} if think else {}), **({"vendor": levers} if levers else {}), **client, **busy,
            **({k: False for k in ("emission_guard", "loop_guard") if entry.get(k) is False}),
            **({"junk_patterns": dict(entry["junk_patterns"])}
               if isinstance(entry.get("junk_patterns"), dict) and not _junk_problems(entry["junk_patterns"]) else {})}


def _junk_problems(raw) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["providers.chat.junk_patterns: not an object of name -> regex"]
    out = []
    for k, v in raw.items():
        try:
            re.compile(v) if isinstance(v, str) and v else None
        except re.error as e:
            out.append(f"providers.chat.junk_patterns.{k}: not a valid regex ({e})")
            continue
        if not isinstance(v, str) or not v:
            out.append(f"providers.chat.junk_patterns.{k}: not a non-empty regex string")
    return out


def output_parts(chunk: dict) -> list[tuple[str, str]]:
    """The model text in a Chat Completions answer or stream chunk, as (kind, text): content,
    reasoning and each tool call's arguments (kind args:<index>), for the junk scan."""
    out = []
    for c in (chunk.get("choices") or []) if isinstance(chunk, dict) else ():
        m = (c.get("message") or c.get("delta") or {}) if isinstance(c, dict) else {}
        if not isinstance(m, dict):
            continue
        for f in ("content",) + REASONING_FIELDS:
            if isinstance(m.get(f), str) and m[f]:
                out.append(("reasoning" if f != "content" else f, m[f]))
        for i, t in enumerate(m.get("tool_calls") or ()):
            a = ((t.get("function") or {}).get("arguments")) if isinstance(t, dict) else None
            if isinstance(a, str) and a:
                out.append((f"args:{t.get('index', i)}", a))
    return out


def tool_input_errors(body: dict) -> list[str]:
    """The ids (else texts) of tool results in a Messages request that report a tool call Claude Code
    rejected for bad input (TOOL_INPUT_RE inside a <tool_use_error>)."""
    out = []
    for m in (body.get("messages") or ()) if isinstance(body, dict) else ():
        if not isinstance(m, dict) or m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if not (isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")):
                continue
            c = b.get("content")
            text = c if isinstance(c, str) else " ".join(
                x.get("text") or "" for x in (c or ()) if isinstance(x, dict))
            if "<tool_use_error>" in text and TOOL_INPUT_RE.search(text):
                out.append(str(b.get("tool_use_id") or text[:200]))
    return out


def failing_streak(body: dict) -> tuple[str, int]:
    """The tool call the request's history ends on, as `name input` (input JSON, keys sorted), and how many
    times in a row it was sent and failed (is_error) at the end of that history; ("", 0) when the last call
    did not fail."""
    calls, failed = [], set()
    for m in (body.get("messages") or ()) if isinstance(body, dict) else ():
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if not isinstance(b, dict):
                continue
            if m.get("role") == "assistant" and b.get("type") == "tool_use":
                calls.append((str(b.get("id")), f"{b.get('name')} {json.dumps(b.get('input'), sort_keys=True)}"))
            elif m.get("role") == "user" and b.get("type") == "tool_result" and b.get("is_error"):
                failed.add(str(b.get("tool_use_id")))
    n, key = 0, ""
    for i, k in reversed(calls):
        if i not in failed or (key and k != key):
            break
        key, n = k, n + 1
    return key, n


def _count_ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def busy_wait(status: int, headers, detail: str, n: int) -> float | None:
    """How long to wait before sending call `n` (1, 2, ...) again after an upstream at capacity, or None
    when the answer is not one to wait out: a 429 (unless the body says waiting cannot help), or a 503
    that names its wait. Retry-After (seconds or an HTTP date) first, then the body's "retry after N
    seconds", else 2**n s; never over BUSY_WAIT_MAX_S."""
    if status not in (429, 503) or NEVER_FITS_RE.search(detail or ""):
        return None
    hint, got = (headers.get("Retry-After") if headers is not None else None), None
    if hint:
        try:
            got = float(str(hint).strip())
        except ValueError:
            try:
                got = email.utils.parsedate_to_datetime(str(hint)).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                got = None
    if got is None and (m := RETRY_AFTER_RE.search(detail or "")):
        got = float(m.group(1))
    if got is None and status == 503:
        return None   # a plain 503 is an outage: the mid-run swap handles those
    return min(max(got if got is not None else 2.0 ** n, BUSY_WAIT_MIN_S), BUSY_WAIT_MAX_S)


def jitter(wait: float) -> float:
    """`wait` plus up to BUSY_JITTER of itself at random, never past BUSY_WAIT_MAX_S."""
    return min(wait * (1.0 + random.uniform(0.0, BUSY_JITTER)), max(wait, BUSY_WAIT_MAX_S))


def window_of(entry: dict, model: str, row: dict | None = None) -> int:
    """The context window for `model`: the run's tier row's (`row`), else a tier row naming that model,
    else providers.chat.context_window; 0 for Claude Code's own."""
    rows = [row or {}] + [r for r in ((entry or {}).get("tiers") or {}).values()
                          if isinstance(r, dict) and r.get("model") == model]
    for r in rows:
        if r.get("context_window"):
            return int(r["context_window"])
    return int((entry or {}).get("context_window") or 0)


def thinking_of(entry: dict, environ=None) -> dict:
    """The run's preserved-thinking settings ({field, max_turns, skip_models}), or {} when off:
    providers.chat.preserve_thinking (true, or an object of those keys), overridden by THINKING_ENV
    ("1" on with the config's keys or the defaults, "0" off). An invalid entry is off."""
    raw = (entry or {}).get("preserve_thinking") if isinstance(entry, dict) else None
    flag = str((os.environ if environ is None else environ).get(THINKING_ENV) or "").strip().lower()
    on = flag in ("1", "true", "on", "yes") or (flag not in ("0", "false", "off", "no") and bool(raw))
    if not on or _thinking_problems(raw):
        return {}
    got = raw if isinstance(raw, dict) else {}
    return {"field": str(got.get("field") or REASONING_FIELDS[0]), "max_turns": int(got.get("max_turns") or 0),
            "skip_models": [str(w) for w in got.get("skip_models", THINKING_SKIP)]}


def vendor_levers(entry: dict, environ=None) -> list[str]:
    """The vendor-default levers on for a run (VENDOR_LEVERS order), [] when none: providers.chat.vendor_defaults
    (true for all, or an object of lever: true), overridden by VENDOR_ENV ("1" all, "0" none, or lever names
    separated by commas, for one run). An invalid entry is off."""
    raw = (entry or {}).get("vendor_defaults") if isinstance(entry, dict) else None
    flag = str((os.environ if environ is None else environ).get(VENDOR_ENV) or "").strip().lower()
    if flag in ("1", "true", "on", "yes", "all"):
        return list(VENDOR_LEVERS)
    if flag in ("0", "false", "off", "no", "none"):
        return []
    if flag:
        named = {w.strip() for w in flag.split(",")}
        return [k for k in VENDOR_LEVERS if k in named]
    if _vendor_problems(raw, (entry or {}).get("vendor_values")):
        return []
    if raw is True:
        return list(VENDOR_LEVERS)
    return [k for k in VENDOR_LEVERS if isinstance(raw, dict) and raw.get(k) is True]


def vendor_values(entry: dict, model: str) -> dict:
    """The published settings of `model`'s family (VENDOR, by the first word its id contains), with
    providers.chat.vendor_values.<word> over them; {} for a model no entry names."""
    over = (entry or {}).get("vendor_values") if isinstance(entry, dict) else None
    over = over if isinstance(over, dict) else {}
    low = str(model or "").lower()
    for word in [*over, *(w for w in VENDOR if w not in over)]:
        if str(word).lower() in low:
            return {**VENDOR.get(str(word).lower(), {}), **(over.get(word) or {})}
    return {}


def vendor_window(entry: dict, model: str, levers) -> int:
    """The model's real context window when the context_window lever is on, else 0 (the tier's own)."""
    return int(vendor_values(entry, model).get("context_window") or 0) if "context_window" in levers else 0


def vendor_client(entry: dict, model: str, levers) -> dict:
    """The run.json keys for the agent's own settings: its client timeout and the attribution header."""
    vals = vendor_values(entry, model)
    timeout = int(vals.get("timeout_ms") or 0) if "timeout" in levers else 0
    return {**({"client_timeout_ms": timeout} if timeout else {}),
            **({"attribution_header": False} if "attribution_header" in levers else {})}


def run_fields(entry: dict, model: str, effort: str, levers=()) -> dict:
    """The request fields for `model` at `effort`: the vendor's sampling and its effort mapped onto the
    levels the model knows (an effort the map lacks takes its `*`), under providers.chat.effort_fields,
    which win."""
    vals = vendor_values(entry, model) if levers else {}
    out = dict(vals.get("sampling") or {}) if "sampling" in levers else {}
    emap = (vals.get("effort_map") or {}) if "effort_map" in levers else {}
    if level := emap.get(effort) or emap.get("*"):
        out["reasoning_effort"] = level
    return {**out, **effort_fields(entry, model, effort)}


def _vendor_problems(raw, values=None) -> list[str]:
    out = []
    if raw is not None and not isinstance(raw, bool) and not (
            isinstance(raw, dict) and all(k in VENDOR_LEVERS and isinstance(v, bool) for k, v in raw.items())):
        out.append(f"providers.chat.vendor_defaults: give true, false or an object of levers ({', '.join(VENDOR_LEVERS)}) "
                   "set to true or false; vendor defaults are off")
    if values is not None and not (isinstance(values, dict) and all(isinstance(v, dict) for v in values.values())):
        out.append("providers.chat.vendor_values: give an object of model id words, each an object of settings "
                   "(sampling, effort_map, context_window, timeout_ms); vendor defaults are off")
    elif isinstance(values, dict):
        for word, row in values.items():
            emap = row.get("effort_map")
            if emap is not None and not (isinstance(emap, dict) and set(emap) <= {*EFFORTS, "*"}
                                         and all(isinstance(v, str) for v in emap.values())):
                out.append(f"providers.chat.vendor_values.{word}.effort_map: give an object of efforts "
                           f"({', '.join(EFFORTS)} or *) to the model's own level names; vendor defaults are off")
            if (samp := row.get("sampling")) is not None and not (isinstance(samp, dict) and not CONVERSATION & set(samp)):
                out.append(f"providers.chat.vendor_values.{word}.sampling: give an object of request fields such as "
                           '{"temperature": 1.0}; vendor defaults are off')
            for key in ("context_window", "timeout_ms"):
                if (v := row.get(key)) is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                    out.append(f"providers.chat.vendor_values.{word}.{key}: give a whole number; vendor defaults are off")
    return out


def _thinking_problems(raw) -> list[str]:
    where = "providers.chat.preserve_thinking"
    if raw is None or isinstance(raw, bool):
        return []
    if not isinstance(raw, dict):
        return [f"{where}: give true, false or an object of field, max_turns and skip_models; it stays off"]
    out = []
    if unknown := sorted(set(raw) - {"field", "max_turns", "skip_models"}):
        out.append(f"{where}: unknown key {', '.join(unknown)}; give field, max_turns or skip_models; it stays off")
    if raw.get("field") is not None and raw["field"] not in REASONING_FIELDS:
        out.append(f"{where}.field: give {' or '.join(REASONING_FIELDS)}; it stays off")
    if raw.get("max_turns") is not None and not (isinstance(raw["max_turns"], int) and not isinstance(raw["max_turns"], bool)
                                                 and raw["max_turns"] >= 0):
        out.append(f"{where}.max_turns: give a whole number of assistant turns (0 = all); it stays off")
    if raw.get("skip_models") is not None and not (isinstance(raw["skip_models"], list)
                                                   and all(isinstance(w, str) and w.strip() for w in raw["skip_models"])):
        out.append(f'{where}.skip_models: give a list of model id words, such as ["flash"]; it stays off')
    return out


def skips_thinking(think: dict, model: str) -> bool:
    """Whether `model` gets no reasoning back under `think` (off, or its id holds a skip_models word)."""
    low = str(model or "").lower()
    return not think or any(w.lower() in low for w in think.get("skip_models") or ())


def keep_reasoning(think: dict, model: str, msgs: list[dict]) -> int:
    """Set each assistant turn's reasoning (its "_reasoning", always removed) in think's field on the turns
    that keep it: none for a skipped model, else the last max_turns (0 = all). Returns the turns that did."""
    turns = [m for m in msgs if m.get("role") == "assistant"]
    on = not skips_thinking(think, model)
    cap = int((think or {}).get("max_turns") or 0)
    kept = 0
    for i, m in enumerate(turns):
        text = m.pop("_reasoning", None)
        if on and text and (not cap or i >= len(turns) - cap):
            m[think["field"]] = text
            kept += 1
    return kept


def _signature(text: str) -> str:
    """A thinking block's signature: Claude Code sends the block back only with one. Opaque to it;
    the bridge reads the text, never the signature, when the turn comes back."""
    return "ttp-" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def keep_template_thinking(think: dict, model: str, req: dict) -> dict:
    """`req` asking the chat template to keep past reasoning (chat_template_kwargs clear_thinking=false,
    unless the request sets clear_thinking itself) when `model` gets reasoning back."""
    if skips_thinking(think, model):
        return req
    kw = req.get("chat_template_kwargs") if isinstance(req.get("chat_template_kwargs"), dict) else {}
    return {**req, "chat_template_kwargs": {"clear_thinking": False, **kw}}


def reasoning_of(obj: dict) -> str:
    """A Chat Completions message's or stream delta's reasoning text (reasoning_content, else reasoning)."""
    for k in ("reasoning_content", "reasoning"):
        if isinstance(obj.get(k), str) and obj[k]:
            return obj[k]
    return ""


def relay_reasoning(think: dict, model: str, msgs: list) -> list:
    """A relayed (/v1) call's messages, copied, each assistant turn's reasoning (either field) moved to
    think's field, kept as keep_reasoning says; other messages unchanged."""
    out = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "assistant":
            text, m = reasoning_of(m), {k: v for k, v in m.items() if k not in REASONING_FIELDS}
            if text:
                m["_reasoning"] = text
        out.append(m)
    keep_reasoning(think, model, [m for m in out if isinstance(m, dict)])
    return out


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
    """`req` with the effort fields set over its own (Claude Code's), never a CONVERSATION field; a
    chat_template_kwargs object in both is merged, the fields' keys winning."""
    out = {**req, **{k: v for k, v in (fields or {}).items() if k not in CONVERSATION}}
    if isinstance(req.get("chat_template_kwargs"), dict) and isinstance(out.get("chat_template_kwargs"), dict):
        out["chat_template_kwargs"] = {**req["chat_template_kwargs"], **out["chat_template_kwargs"]}
    return out


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
    out += _thinking_problems(entry.get("preserve_thinking"))
    out += _junk_problems(entry.get("junk_patterns"))
    for k, what in (("emission_guard", "mend unparsed tool arguments"), ("loop_guard", "nudge, then stop, a failing loop")):
        if entry.get(k) not in (None, True, False):
            out.append(f"providers.chat.{k}: give true or false (default true: {what})")
    out += _vendor_problems(entry.get("vendor_defaults"), entry.get("vendor_values"))
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
    for key in ("busy_retries", "busy_wait_s"):
        if entry.get(key) is not None and not (_count_ok(entry[key]) and (key == "busy_wait_s" or isinstance(entry[key], int))):
            out.append(f"providers.chat.{key}: give a whole number {'of retries' if key == 'busy_retries' else 'of seconds'} "
                       f"(0 = no wait); the default ({BUSY_RETRIES if key == 'busy_retries' else int(BUSY_WAIT_S)}) applies")
    for key, unset in (("max_tokens", "none sent: the client's own is dropped"), ("context_window", "Claude Code's own")):
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


def to_chat(body: dict, model: str, cap: int = 0, think: dict | None = None) -> dict:
    """A Chat Completions request for a Messages request, sent to `model`. Only `cap`, the user's own
    max_tokens setting, is sent: the client's max_tokens is dropped, so a reasoning model is never cut off.
    With `think` (thinking_of), assistant turns carry their thinking blocks' text (keep_reasoning)."""
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
            thought = "\n".join(str(b.get("thinking") or "") for b in blocks
                                if b.get("type") == "thinking" and b.get("thinking"))
            if thought and think:
                msg["_reasoning"] = thought   # placed (or dropped) by keep_reasoning below
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
    if think:
        keep_reasoning(think, model, msgs)
    req: dict = keep_template_thinking(think, model, {"model": model, "messages": msgs}) if think else {
        "model": model, "messages": msgs}
    if cap:
        req["max_tokens"] = int(cap)
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


def args_unparsed(text: str) -> bool:
    """Whether a tool call's arguments text is not empty yet holds no JSON object (tool_input gives {})."""
    text = str(text or "").strip()
    if not text:
        return False
    try:
        got, _ = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return True
    return not isinstance(got, dict)


def repair_args(text) -> dict | None:
    """The emission guard's mend of tool arguments that do not parse (the module doc), or None when
    none gives an object: strings are copied untouched, a trailing comma outside them is dropped, and
    missing closers are added only when the text ends after a whole value (never inside a string, a
    number or a word, which may have been cut short)."""
    t = str(text or "").strip()
    if (m := re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)):
        t = m.group(1)
    out, closers, in_str, esc = [], [], False, False
    for ch in t:
        if in_str:
            esc, in_str = (False, True) if esc else (ch == "\\", ch != '"')
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            closers.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if closers:
                closers.pop()
            while out and out[-1].isspace():
                out.pop()
            if out and out[-1] == ",":
                out.pop()
        out.append(ch)
    body = "".join(out).rstrip()
    if in_str or (closers and body[-1:] not in ('"', "}", "]", ",", "{", "[")):
        return None   # cut inside a string, a number or a word: the value may be short
    text = body.rstrip(",") + "".join(reversed(closers))
    try:
        got, _ = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def tool_input(arguments, repair: bool = False) -> dict:
    """A tool call's arguments as an object. A model that sends two JSON objects back to back gets
    the first one; text that is no object at all gives {}, or with `repair` (the emission guard)
    repair_args's mend when it has one."""
    if isinstance(arguments, dict):
        return arguments
    text = str(arguments or "").strip()
    if not text:
        return {}
    try:
        got, _ = json.JSONDecoder().raw_decode(text)
    except ValueError:
        got = None
    if not isinstance(got, dict) and repair:
        got = repair_args(text)
    return got if isinstance(got, dict) else {}


def usage_of(u: dict | None) -> dict:
    u = u or {}
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    return {"input_tokens": max(int(u.get("prompt_tokens") or 0) - cached, 0),
            "output_tokens": int(u.get("completion_tokens") or 0),
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}


def _msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def from_chat(resp: dict, model: str, thinking: bool = False, repair: bool = False) -> dict:
    """The Messages response for a non-streamed Chat Completions response: its reasoning a thinking
    block first when `thinking` (preserved thinking), else dropped."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    if thinking and (thought := reasoning_of(msg)):
        content.append({"type": "thinking", "thinking": thought, "signature": _signature(thought)})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        content.append({"type": "tool_use", "id": c.get("id") or "toolu_" + uuid.uuid4().hex[:24],
                        "name": fn.get("name"), "input": tool_input(fn.get("arguments"), repair)})
    stop = "tool_use" if msg.get("tool_calls") else STOP_REASONS.get(choice.get("finish_reason") or "stop", "end_turn")
    return {"id": _msg_id(), "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": usage_of(resp.get("usage"))}


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Stream:
    """Turns Chat Completions stream chunks into Messages SSE events. Text streams as it comes;
    tool calls are gathered by their index (ids matched, arguments joined, interleaved calls kept
    apart) and sent whole, in index order, when the upstream stream ends."""

    def __init__(self, model: str, thinking: bool = False):
        self.repair = False   # the emission guard (Bridge.emission_guard)
        self.model, self.index, self.text_open, self.started = model, 0, False, False
        self.thinking, self.thought, self.thoughts = thinking, None, 0   # preserved thinking: open block's text, blocks
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
            thought = reasoning_of(delta) if self.thinking else ""
            if thought:
                if self.thought is None:
                    out += self._close_text()
                    out += _sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                        "content_block": {"type": "thinking", "thinking": "",
                                                                          "signature": ""}})
                    self.thought = ""
                self.thought += thought
                out += _sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                    "delta": {"type": "thinking_delta", "thinking": thought}})
            text = delta.get("content")
            if text or delta.get("tool_calls"):
                out += self._close_thinking()
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

    def _close_thinking(self) -> bytes:
        """The open thinking block's signature and end (Claude Code keeps a signed block to send back)."""
        if self.thought is None:
            return b""
        sig, self.thought = _signature(self.thought), None
        self.index, self.thoughts = self.index + 1, self.thoughts + 1
        return (_sse("content_block_delta", {"type": "content_block_delta", "index": self.index - 1,
                                             "delta": {"type": "signature_delta", "signature": sig}})
                + _sse("content_block_stop", {"type": "content_block_stop", "index": self.index - 1}))

    def _close_text(self) -> bytes:
        if not self.text_open:
            return b""
        self.text_open = False
        self.index += 1
        return _sse("content_block_stop", {"type": "content_block_stop", "index": self.index - 1})

    def end(self) -> bytes:
        out = (b"" if self.started else self.start()) + self._close_thinking() + self._close_text()
        for i in sorted(self.calls):
            c = self.calls[i]
            out += _sse("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": {
                "type": "tool_use", "id": c["id"] or "toolu_" + uuid.uuid4().hex[:24], "name": c["name"], "input": {}}})
            out += _sse("content_block_delta", {"type": "content_block_delta", "index": self.index, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(tool_input(c["args"], self.repair))}})
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
    if status == 403 and challenged(detail):
        # A gateway's challenge page is the network path, not the key: retryable, never a logout.
        kind, code, msg = "overloaded_error", 529, "a gateway in front of the upstream endpoint answered with a challenge page"
    elif status in (401, 403):
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
    request itself. A 403 that is a gateway's challenge page (challenged) is an error, not auth."""
    if status == 403 and challenged(detail):
        return "error"
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

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (_ClientGone, *CLIENT_GONE):   # one line, no traceback: the caller left (e.g. its own timeout)
            self.close_connection = True
            print("chatbridge: the client hung up before its answer was written", file=sys.stderr, flush=True)
        finally:   # a host in-flight slot a path left held (inflight.py): freed with its request
            slot, self.ttp_slot = getattr(self, "ttp_slot", None), None
            if slot is not None:
                slot.release()

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except CLIENT_GONE as e:
            raise _ClientGone() from e

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
        except CLIENT_GONE as e:
            raise _ClientGone() from e
        self._write(data)

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
        if path == "/v1/chat/completions":   # an OpenAI-native agent (a bench harness arm): passed through
            return self.bridge.relay(self, body)
        if path != "/v1/messages":
            return self.do_GET()
        self.bridge.forward(self, body)


class Bridge:
    """One run's bridge: start() binds 127.0.0.1:<ephemeral> and serves in a daemon thread; stop()
    closes the port. Use as a context manager."""

    def __init__(self, base_url: str, key: str, model: str, max_tokens: int = 0,
                 timeout: float = UPSTREAM_TIMEOUT_S, context_window: int = 0, effort: str = "",
                 fields: dict | None = None, standins: list | None = None, served: list | None = None,
                 busy_retries: int | None = None, busy_wait_s: float | None = None, thinking: dict | None = None,
                 junk_patterns: dict | None = None, client_timeout_ms: int = 0, attribution_header: bool = True,
                 vendor: list | None = None, emission_guard: bool = True, loop_guard: bool = True):
        self.base_url, self._key, self.model = endpoint.base_url(base_url), key, model
        self.emission_guard = bool(emission_guard)   # mend unparsed tool arguments (repair_args)
        self.loop_guard, self._nudged = bool(loop_guard), {}   # the loop guard; each call it nudged: failures then
        self.loop = {"nudges": 0, "stops": 0, "bounce": ""}   # stats()["loop"]
        self.vendor = list(vendor or [])   # the vendor-default levers on (vendor_levers), for stats()
        # The agent's own call timeout (API_TIMEOUT_MS); the bridge waits upstream at least as long.
        self.client_timeout_ms, self.attribution_header = int(client_timeout_ms or 0), bool(attribution_header)
        timeout = max(timeout, self.client_timeout_ms / 1000.0)
        self.thinking = dict(thinking or {})   # preserved thinking (thinking_of), {} when off
        self.thought = {"returned": 0, "sent": 0}   # reasoning turns given to the client / sent back upstream
        self.max_tokens, self.timeout = int(max_tokens or 0), timeout
        self.context_window = int(context_window or 0)
        self.effort, self.fields, self.rejected = effort, dict(fields or {}), False   # effort_fields
        self.token = secrets.token_urlsafe(24)
        self.last_fault, self.faults = "", {}   # the last call's endpoint fault kind (fault_of), counts
        self.upstream: dict = {}   # the last failed answer's HTTP status and error code (upstream_of)
        self._listed: bool | None | str = ""    # model_listed's answer, "" until asked
        self.standins = [s for s in standins or () if isinstance(s, dict) and s.get("model")]   # ranked, best first
        self.served = [str(d) for d in served or ()]   # state dirs whose served list (route.MODELS_FILE) is read
        self.mid_run: list[dict] = []   # each swap: {from, to, at, why}
        self.on_swap = None   # called with {asked, ran} after each swap (the runner's live SWAP_FILE)
        self._errs, self._left, self._swap_lock = 0, set(), threading.Lock()   # 5xx/no-answer run; models left
        self.busy_retries = BUSY_RETRIES if busy_retries is None else int(busy_retries)
        self.busy_wait_s = BUSY_WAIT_S if busy_wait_s is None else float(busy_wait_s)
        # Passed-through OpenAI calls' usage (relay): their agents report none Claude-style, so the bridge counts.
        # `scanned`: answers _scan read for bad output, so a run's zero counts are told from a run never measured.
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "scanned": 0}
        self.busy = {"waits": 0, "waited_s": 0.0, "gave_up": 0}   # capacity waits (busy_wait), for stats()
        self.quota_until = None   # the first quota 429's window end (quota_until): the runner stops on it
        # After this many model_missing answers, later calls get the 404 here, never upstream (0: no cap; a
        # bench row sets it so a withdrawn model's agents stop calling the endpoint: tbcycle.MISSING_MAX).
        self.missing_max, self.missing_local = 0, 0
        self.calls = serverhealth.Calls()   # per call: server wait, output tokens, tokens/s (stats()["server"])
        self.inflight = inflight.Waits()   # the host in-flight cap's slot waits (stats()["inflight"])
        self._jitter = jitter
        jp = JUNK_PATTERNS if junk_patterns is None or _junk_problems(junk_patterns) else junk_patterns
        self.junk = {k: re.compile(v) for k, v in jp.items()}
        self.bad = {"tool_input_errors": 0, "args_unparsed": 0, "empty_content": 0, "junk_responses": 0,
                    "args_repaired": 0, "junk_hits": {}}   # stats()["bad_output"]
        self._bad_seen: set[str] = set()   # tool_use ids counted (Claude Code resends the whole history)
        self._sleep = time.sleep
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
                      "served": s.get("served") or [], "thinking": s.get("thinking") or {},
                      "junk_patterns": s.get("junk_patterns"),
                      "client_timeout_ms": s.get("client_timeout_ms") or 0, "vendor": s.get("vendor") or [],
                      "attribution_header": s.get("attribution_header", True) is not False,
                      "emission_guard": s.get("emission_guard", True) is not False,
                      "loop_guard": s.get("loop_guard", True) is not False,
                      **{k: s[k] for k in ("busy_retries", "busy_wait_s") if _count_ok(s.get(k))}, **kw})

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}" if self.server else ""

    def env(self) -> dict[str, str]:
        """What the agent gets to reach the bridge: its URL and the per-run token, never the key."""
        return {"ANTHROPIC_BASE_URL": self.url, "ANTHROPIC_AUTH_TOKEN": self.token, "ANTHROPIC_API_KEY": "",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                # Claude Code takes 200k for a model it does not know: compact within the tier's window.
                **({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(self.context_window)} if self.context_window else {}),
                # The vendor defaults (vendor_client): a long call timeout, and no per-request attribution
                # header, which changes the system prompt each call and so defeats the server's prefix cache.
                **({"API_TIMEOUT_MS": str(self.client_timeout_ms)} if self.client_timeout_ms else {}),
                **({} if self.attribution_header else {"CLAUDE_CODE_ATTRIBUTION_HEADER": "0"})}

    def openai_env(self) -> dict[str, str]:
        """What an OpenAI-compatible agent (a bench harness arm) gets: the bridge's /v1 relay and the run token."""
        return {"OPENAI_BASE_URL": self.url + "/v1", "OPENAI_API_BASE": self.url + "/v1", "OPENAI_API_KEY": self.token}

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
        if kind == "error" and status == 404 and self.model_listed() is False:
            kind = "model_missing"   # a bare 404, and the endpoint's /models leaves the model out
        self.last_fault = kind
        self.upstream = upstream_of(status, detail, self._key)
        with self._swap_lock:   # calls in parallel count their errors in a row, none lost
            self._errs = self._errs + 1 if status == 0 or status >= 500 else 0
        if kind:
            self.faults[kind] = self.faults.get(kind, 0) + 1

    def model_listed(self) -> bool | None:
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
                **({"quota_until": self.quota_until} if self.quota_until else {}),
                **({"effort": self.effort, "fields": dict(self.fields), "rejected": self.rejected}
                   if self.fields else {}),
                **({"mid_run": [dict(m) for m in self.mid_run], "model": self.model} if self.mid_run else {}),
                **({"busy": {**self.busy, "waited_s": round(self.busy["waited_s"], 1)}} if self.busy["waits"]
                   or self.busy["gave_up"] else {}),
                **({"usage": dict(self.usage)} if self.usage["calls"] else {}),
                **({"missing_local": self.missing_local} if self.missing_local else {}),
                **({"thinking": {"field": self.thinking.get("field"), **self.thought}} if self.thinking else {}),
                **({"server": self.calls.export()} if self.calls.rows else {}),
                **({"inflight": self.inflight.export()} if self.inflight else {}),
                **({"bad_output": {**self.bad, "junk_hits": dict(self.bad["junk_hits"])}}
                   if any(v for k, v in self.bad.items() if k != "junk_hits") else {}),
                **({"vendor": list(self.vendor)} if self.vendor else {}),
                **({"loop": dict(self.loop)} if self.loop["nudges"] or self.loop["stops"] else {})}

    def server_wait_s(self) -> float:
        """The time spent waiting on the server so far (in-flight calls included): the cell limit does not count it."""
        return self.calls.wait_s()

    def _timed_open(self, h, req: dict):
        """_open, timed as one call of `h` (its first byte is marked when the stream's first line is read),
        holding one of the host's in-flight slots (inflight.py) until the call ends (_end_call)."""
        self._end_call(h, 0, "other")   # an earlier attempt of the same request that failed
        slot = inflight.acquire(self.timeout)
        with self._swap_lock:
            if slot is None:
                self.inflight.timeouts += 1
            elif slot.index >= 0:
                self.inflight.add(slot.waited_s)
        if slot is None:   # the host's cap stayed full: an overload answer, never sent upstream
            msg = f"overloaded: the host's chat in-flight cap ({inflight.KEY}) had no free slot in {self.timeout:.0f} s"
            raise urllib.error.HTTPError("inflight", 529, "overloaded", None,   # type: ignore[arg-type]
                                         io.BytesIO(json.dumps({"error": {"message": msg}}).encode()))
        if slot.waited_s:
            self.calls.sleep_wait(slot.waited_s)
        call = self.calls.begin()
        try:
            resp = self._open(req)
        except BaseException as e:   # a 429 or 503 (the codes busy_wait waits out) is the server at capacity
            slot.release()
            code = getattr(e, "code", 0)
            if code in inflight.REFUSALS and inflight.refused():   # the host backs off at once
                with self._swap_lock:
                    self.inflight.refusals += 1
            inflight.record_call(code if isinstance(code, int) else 0)
            call.end(0, "capacity" if code in (429, 503) else "other")
            raise
        h.ttp_call, h.ttp_slot = call, slot
        return resp

    def _end_call(self, h, out: int, status: str = "ok") -> None:
        call, h.ttp_call = getattr(h, "ttp_call", None), None
        slot, h.ttp_slot = getattr(h, "ttp_slot", None), None
        if slot is not None:
            slot.release()
        if call is not None:
            call.end(out, status)
            inflight.record("ok" if status == "ok" else "error")

    def _open(self, req: dict):
        r = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(req).encode(), headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
            "Accept": "text/event-stream" if req.get("stream") else "application/json"})
        return urllib.request.build_opener(endpoint._NoRedirect).open(r, timeout=self.timeout)

    def _busy_wait(self, e: urllib.error.HTTPError, detail: str, tries: int, waited: float) -> float | None:
        """The wait before this call goes out again after a capacity answer, None to pass the answer
        on, or -1.0 once past the call's retries or wait budget (given up: a capacity stop)."""
        wait = busy_wait(e.code, e.headers, detail, tries + 1)
        if wait is None or self.busy_retries <= 0 or self.busy_wait_s <= 0:   # 0: no waits (the answer as it came)
            return None
        wait = self._jitter(wait)
        with self._swap_lock:
            if tries >= self.busy_retries or waited + wait > self.busy_wait_s:
                self.busy["gave_up"] += 1
                return -1.0
            self.busy["waits"] += 1
            self.busy["waited_s"] += wait
        return wait

    def _send(self, h: _Handler, make) -> tuple | None:
        """The upstream answer to the call `make(model)` builds, after capacity waits, a rejected effort
        field's retry and swaps: (response, request sent), or None once an error was answered to `h`."""
        tries, waited = 0, 0.0   # this call's capacity waits (busy_wait)
        while True:
            with self._swap_lock:   # one model and its own fields: a swap in another call changes all three
                used, fields, rejected = self.model, dict(self.fields), self.rejected
            if self._errs >= SWAP_ERRORS and self._swapped(used):   # e.g. a stream that broke 3 times
                continue
            if self.missing_max and self.faults.get("model_missing", 0) >= self.missing_max:
                with self._swap_lock:
                    self.missing_local += 1
                h._reply(*error_for(404, f"the endpoint does not serve {used} (answered by the bridge after "
                                         f"{self.missing_max} upstream answers said so)"))
                return None
            req = make(used)
            try:
                try:
                    resp = self._timed_open(h, with_fields(req, {} if rejected else fields))
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
                    resp = self._timed_open(h, req)
            except urllib.error.HTTPError as e:
                detail = getattr(e, "ttp_detail", None)
                if detail is None:
                    try:
                        detail = e.read(2000).decode("utf-8", "replace")
                    except Exception:
                        detail = ""
                if e.code == 429 and self.quota_until is None:
                    self.quota_until = quota_until(detail)
                wait = self._busy_wait(e, detail, tries, waited)
                if wait is not None and wait >= 0:
                    tries, waited = tries + 1, waited + wait
                    self.calls.sleep_wait(wait)
                    self._sleep(wait)
                    continue
                self._fault(e.code, detail)
                if self._swapped(used):
                    continue
                if wait is not None:   # waited out as far as allowed: a capacity stop, not the model's
                    detail = f"the model stayed at capacity after {tries} retries over {waited:.0f} s: {detail}"
                h._reply(*error_for(e.code, self._scrub(detail)))
                return None
            except (urllib.error.URLError, OSError) as e:
                self._fault(0)
                if self._swapped(used):
                    continue
                h._reply(*error_for(0, self._scrub(str(getattr(e, "reason", e)))))
                return None
            return resp, req

    def relay(self, h: _Handler, body: dict) -> None:
        """An OpenAI chat-completions call passed through as asked (its own tools and messages), on the
        run's model and effort fields, with the same waits, swaps and fault records as forward; its usage
        is counted (self.usage), a stream asking the endpoint to report it."""
        def make(used: str) -> dict:
            req = {**body, "model": used}
            if self.thinking and isinstance(body.get("messages"), list):
                req = keep_template_thinking(self.thinking, used, {
                    **req, "messages": relay_reasoning(self.thinking, used, body["messages"])})
            if req.get("stream"):
                req["stream_options"] = {**(req.get("stream_options") or {}), "include_usage": True}
            return req
        got = self._send(h, make)
        if got is None:
            return
        resp, req = got
        self._sent_back(req)
        with resp:
            if not req.get("stream"):
                try:
                    data = json.loads(resp.read())
                except ValueError:
                    self._end_call(h, 0, "parse")
                    return h._reply(*self._malformed("the upstream answer is not JSON"))
                if data.get("error"):
                    self._end_call(h, 0, "other")
                    return h._reply(*self._fault_reply(_chunk_error(data, self._scrub), data))
                self._ok()
                self._count(data.get("usage"))
                self._end_call(h, usage_of(data.get("usage"))["output_tokens"])
                self._scan(output_parts(data))
                return h._reply(200, data)
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Cache-Control", "no-cache")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            usage, seen, status, parts = None, 0, "ok", []
            try:
                for raw in resp:
                    if getattr(h, "ttp_call", None) is not None:
                        h.ttp_call.byte()
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                        seen += len(line)
                        try:
                            obj = json.loads(line[5:]) or {}
                            usage = obj.get("usage") or usage
                            parts += output_parts(obj)
                        except (ValueError, AttributeError):
                            pass
                    if line.startswith("data:"):   # no `event:` or comment lines: an OpenAI client reads them as broken data
                        data = (line + "\n\n").encode()
                        h._write(b"%x\r\n%s\r\n" % (len(data), data))
                h._write(b"0\r\n\r\n")
                self._ok()
            except OSError:   # the upstream stream broke (a client hang-up is _ClientGone: handle_one_request)
                self._fault(0)
                status = "stream"
            finally:
                self._count(usage)
                self._scan(parts)
                self._end_call(h, usage_of(usage)["output_tokens"] or seen // 120, status)

    def _count(self, u) -> None:
        if not isinstance(u, dict):
            return
        got = usage_of(u)
        with self._swap_lock:
            self.usage["calls"] += 1
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                self.usage[k] += int(got.get(k) or 0)

    def _sent_back(self, req: dict) -> None:
        """Count the assistant turns whose reasoning went upstream (preserved thinking, stats())."""
        if self.thinking:
            n = sum(1 for m in req.get("messages") or () if isinstance(m, dict) and m.get("role") == "assistant"
                    and m.get(self.thinking["field"]))
            with self._swap_lock:
                self.thought["sent"] += n

    def _rejected(self, body: dict) -> None:
        """Count the tool calls Claude Code rejected for bad input since the last request (stats())."""
        new = [i for i in tool_input_errors(body) if i not in self._bad_seen]
        if new:
            with self._swap_lock:
                self._bad_seen.update(new)
                self.bad["tool_input_errors"] += len(set(new))

    def _scan(self, parts) -> None:
        """Count one answer's bad output (its (kind, text) parts, joined per kind; stats()): junk by pattern,
        tool arguments that do not parse, and an answer that is reasoning only."""
        with self._swap_lock:
            self.usage["scanned"] += 1
        if not parts:
            return
        by: dict[str, list[str]] = {}
        for kind, text in parts:
            by.setdefault(kind, []).append(text)
        texts = {k: "".join(v) for k, v in by.items()}
        bad_args = [t for k, t in texts.items() if k.startswith("args:") and args_unparsed(t)]
        unparsed = len(bad_args)
        repaired = sum(1 for t in bad_args if repair_args(t) is not None) if self.emission_guard else 0
        empty = not any(k == "content" or k.startswith("args:") for k in texts)
        hits = [k for k, rx in self.junk.items() if any(rx.search(t) for t in texts.values())]
        if hits or unparsed or empty:
            with self._swap_lock:
                self.bad["args_unparsed"] += unparsed
                self.bad["args_repaired"] += repaired
                self.bad["empty_content"] += int(empty)
                self.bad["junk_responses"] += int(bool(hits))
                for k in hits:
                    self.bad["junk_hits"][k] = self.bad["junk_hits"].get(k, 0) + 1

    def _returned(self, n: int) -> None:
        if n:
            with self._swap_lock:
                self.thought["returned"] += n

    def _looped(self, h: _Handler, body: dict) -> bool:
        """The loop guard (the module doc): nudge `body` once per looping call; True when it answered `h`
        with the stop instead."""
        key, n = failing_streak(body) if self.loop_guard else ("", 0)
        if n < LOOP_REPEATS:
            return False
        if key in self._nudged:
            if n <= self._nudged[key]:   # the history the nudge went with, sent again (a retry, a side call)
                return False
            why = f"loop: the same tool call failed {n} times in a row, again after a nudge: {key[:200]}"
            with self._swap_lock:
                self.loop["stops"] += 1
                self.loop["bounce"] = why
            h._reply(*error_for(400, f"stopped by the harness loop guard: {why}"))
            return True
        self._nudged[key] = n
        with self._swap_lock:
            self.loop["nudges"] += 1
        last = (body.get("messages") or [None])[-1]
        if isinstance(last, dict) and isinstance(last.get("content"), list):
            last["content"] = [*last["content"], {"type": "text", "text": LOOP_NUDGE.format(n=n)}]
        return False

    def forward(self, h: _Handler, body: dict) -> None:
        self._rejected(body)
        if self._looped(h, body):
            return
        got = self._send(h, lambda used: to_chat(body, used, self.max_tokens, self.thinking))
        if got is None:
            return
        resp, req = got
        self._sent_back(req)
        think = not skips_thinking(self.thinking, req.get("model") or self.model)   # inbound: same guard
        with resp:
            if not req.get("stream"):
                try:
                    data = json.loads(resp.read())
                except ValueError:
                    self._end_call(h, 0, "parse")
                    return h._reply(*self._malformed("the upstream answer is not JSON"))
                if data.get("error"):
                    self._end_call(h, 0, "other")
                    return h._reply(*self._fault_reply(_chunk_error(data, self._scrub), data))
                self._ok()
                self._count(data.get("usage"))   # a translated call is counted too (cached apart: usage_of)
                self._end_call(h, usage_of(data.get("usage"))["output_tokens"])
                self._scan(output_parts(data))
                out = from_chat(data, body.get("model") or self.model, think, self.emission_guard)
                self._returned(sum(1 for b in out["content"] if b.get("type") == "thinking"))
                return h._reply(200, out)
            st = Stream(body.get("model") or self.model, think)
            st.repair = self.emission_guard
            try:
                self._stream(h, resp, st)
            finally:
                self._returned(st.thoughts)

    def _stream(self, h: _Handler, resp, st: Stream) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()

        def send(data: bytes) -> None:
            if data:
                h._write(b"%x\r\n%s\r\n" % (len(data), data))

        errored, seen, parts = "", 0, []   # the failed call's kind (serverhealth.FAILED_KINDS) once the stream fails
        try:
            for raw in resp:
                if getattr(h, "ttp_call", None) is not None:
                    h.ttp_call.byte()
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                seen += len(line)
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
                    errored = "parse"
                    break
                if chunk.get("error"):
                    code, err = self._fault_reply(_chunk_error(chunk, self._scrub), chunk)
                    send(_sse("error", err))
                    errored = "stream"
                    break
                parts += output_parts(chunk)
                send(st.feed(chunk))
            if not errored:
                self._ok()
                send(st.end())
        except _ClientGone:   # the client hung up: no upstream fault; handle_one_request logs it
            self._count(st.usage or None)
            self._scan(parts)
            self._end_call(h, usage_of(st.usage)["output_tokens"] or seen // 120)
            raise
        except (OSError, socket.timeout) as e:
            self._end_call(h, 0, "stream")
            self._fault(0)
            try:   # the upstream stream broke: say so in the stream, as the API does
                send(_sse("error", error_for(0, self._scrub(str(e)))[1]))
                h._write(b"0\r\n\r\n")
            except (OSError, _ClientGone):
                pass
            h.close_connection = True
            return
        self._count(st.usage or None)
        self._scan(parts)
        self._end_call(h, usage_of(st.usage)["output_tokens"] or seen // 120, errored or "ok")
        self._returned(st.thoughts)   # counted before the stream's end reaches the client (forward adds the rest)
        st.thoughts = 0
        h._write(b"0\r\n\r\n")


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
