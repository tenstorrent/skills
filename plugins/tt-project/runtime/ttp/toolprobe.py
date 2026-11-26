# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp bench toolprobe --n N`: N strict-schema tool-call probes against the chat endpoint.

Each probe asks the model (the project's providers.chat endpoint and tier model, or --endpoint and
--model) for one tool call on a fixed set of strict tools (every property required, no extra ones),
naming the tool in the prompt with tool_choice auto, one call in flight at a time and under the host's in-flight cap
(inflight.py). A refusal (429, 503, 529) lowers the host's level at once and is waited out with
exponential backoff and jitter, never sent again at once; past RETRIES it counts as refused.

Each call's arguments are validated against its tool's schema: with jsonschema from a virtual
environment (this one when it is a venv, else the newest checks env under the account home), never
the user's site-packages; with none, a built-in check of the same strict subset (type, required,
additionalProperties false, enum, minimum). The answer's reasoning, text and arguments are scanned
for junk (chatbridge.JUNK_PATTERNS or providers.chat.junk_patterns).

Output: one JSON object {n, model, valid, invalid, no_tool_call, wrong_tool (another tool than the one
named, still validated against its own schema), refused, errors, junk_responses,
junk_hits, validator, inflight, problems}. The key and URL are never printed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import chatbridge, endpoint, glmquota, inflight

RETRIES = 4   # refusals waited out per probe before it counts as refused
TIMEOUT_S = 300.0
STRICT_TOOLS = [
    {"name": "read_file", "description": "Read part of a text file.",
     "parameters": {"type": "object", "additionalProperties": False, "required": ["path", "start_line", "limit"],
                    "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
                                   "limit": {"type": "integer", "minimum": 1}}}},
    {"name": "run_command", "description": "Run a shell command.",
     "parameters": {"type": "object", "additionalProperties": False, "required": ["command", "timeout_s", "cwd"],
                    "properties": {"command": {"type": "string"}, "timeout_s": {"type": "integer", "minimum": 1},
                                   "cwd": {"type": "string"}}}},
    {"name": "edit_file", "description": "Replace text in a file.",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["path", "old_text", "new_text", "replace_all"],
                    "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                   "new_text": {"type": "string"}, "replace_all": {"type": "boolean"}}}},
    {"name": "set_status", "description": "Record a task's status.",
     "parameters": {"type": "object", "additionalProperties": False, "required": ["task", "status", "notes"],
                    "properties": {"task": {"type": "integer"},
                                   "status": {"type": "string", "enum": ["queued", "running", "done", "failed"]},
                                   "notes": {"type": "array", "items": {"type": "string"}}}}},
]
PROMPTS = [   # (prompt, the tool it names)
    ("Read lines 40 to 59 of src/app/main.py.", "read_file"),
    ("Run the unit tests with `make test` in the repository root /work/repo, allowing 600 seconds.", "run_command"),
    ("In docs/README.md replace every 'colour' with 'color'.", "edit_file"),
    ("Mark task 17 as done, with the notes 'tests pass' and 'docs updated'.", "set_status"),
    ("Show me the first 25 lines of config/settings.yaml.", "read_file"),
    ("List the files in /tmp/build with `ls -la`, giving it 30 seconds.", "run_command"),
    ("In setup.cfg change the line 'version = 1.2.0' to 'version = 1.3.0', once.", "edit_file"),
    ("Task 4 failed: record that, with the note 'timeout on step 3'.", "set_status"),
]
_VALIDATE = r"""
import json, sys
import jsonschema
items = json.load(sys.stdin)
print(json.dumps([[e.message for e in jsonschema.Draft202012Validator(s).iter_errors(a)][:3] for a, s in items]))
"""


def schemas() -> dict:
    return {t["name"]: t["parameters"] for t in STRICT_TOOLS}


def request(prompt: str, tool: str, model: str, strict: bool = True) -> dict:
    """One probe: tool_choice auto with the tool named in the prompt (some endpoints refuse a forced
    tool_choice, a named function or "required", with a 400)."""
    return {"model": model, "messages": [
        {"role": "system", "content": "You operate tools. Answer with exactly one tool call."},
        {"role": "user", "content": f"{prompt}\n\nUse the {tool} tool for this, in one call."}],
        "tools": [{"type": "function", "function": {**t, **({"strict": True} if strict else {})}}
                  for t in STRICT_TOOLS],
        "tool_choice": "auto", "stream": False}


def builtin_errors(value, schema: dict) -> list[str]:
    """The strict subset jsonschema would check here: type, enum, minimum, required, additionalProperties
    false, array items."""
    t = schema.get("type")
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{value!r} is not of type 'integer'"]
    elif t in kinds and (not isinstance(value, kinds[t]) or (t != "boolean" and isinstance(value, bool))):
        return [f"{value!r} is not of type {t!r}"]
    out = []
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{value!r} is not one of {schema['enum']!r}")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        out.append(f"{value!r} is less than the minimum of {schema['minimum']}")
    if t == "object":
        props = schema.get("properties") or {}
        out += [f"{k!r} is a required property" for k in schema.get("required") or () if k not in value]
        if schema.get("additionalProperties") is False:
            out += [f"additional property {k!r} is not allowed" for k in value if k not in props]
        for k, v in value.items():
            if k in props:
                out += builtin_errors(v, props[k])
    if t == "array" and isinstance(schema.get("items"), dict):
        for v in value:
            out += builtin_errors(v, schema["items"])
    return out


def _venv_python() -> str:
    """A Python with jsonschema in a virtual environment: this one when it is a venv, else the newest
    checks env's; '' for none (never the user's site-packages)."""
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return sys.executable
    from .project import HOME_DIR
    envs = sorted((p for p in (HOME_DIR / "checks-env").glob("*/bin/python3") if p.is_file()),
                  key=lambda p: p.stat().st_mtime, reverse=True) if (HOME_DIR / "checks-env").is_dir() else []
    return str(envs[0]) if envs else ""


def validate(items: list, python: str | None = None) -> tuple[list[list[str]], str]:
    """Each (args, schema) pair's errors, and the validator used (jsonschema or builtin)."""
    py = _venv_python() if python is None else python
    if py and items:
        try:
            r = subprocess.run([py, "-I", "-c", _VALIDATE], input=json.dumps(items), capture_output=True,
                               text=True, timeout=120)
            got = json.loads(r.stdout) if r.returncode == 0 else None
            if isinstance(got, list) and len(got) == len(items):
                return got, "jsonschema"
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return [builtin_errors(a, s) for a, s in items], "builtin"


def _jitter_wait(n: int, headers=None, detail: str = "") -> float:
    got = chatbridge.busy_wait(429, headers, detail, n)
    return chatbridge.jitter(got if got is not None else min(2.0 ** n, chatbridge.BUSY_WAIT_MAX_S))


def probe(n: int, post, model: str, junk: dict | None = None, sleep=time.sleep, python: str | None = None) -> dict:
    """Run `n` probes, one at a time. `post(body) -> dict` sends one request and returns the parsed
    answer, raising urllib.error.HTTPError for an HTTP error."""
    waits = inflight.Waits()
    rx = {k: re.compile(v) for k, v in (chatbridge.JUNK_PATTERNS if junk is None else junk).items()}
    out = {"n": n, "model": model, "valid": 0, "invalid": 0, "no_tool_call": 0, "wrong_tool": 0, "refused": 0, "errors": 0,
           "junk_responses": 0, "junk_hits": {}, "problems": []}
    items, strict = [], True
    for i in range(n):
        (prompt, tool), data, failed = PROMPTS[i % len(PROMPTS)], None, "errors"
        for k in range(1, RETRIES + 2):
            wait = 0.0
            slot = inflight.acquire(TIMEOUT_S, sleep=sleep)
            if slot is None:
                waits.timeouts += 1
                out["problems"].append("no free in-flight slot (chat.max_inflight)")
                break
            waits.add(slot.waited_s)
            try:
                data = post(request(prompt, tool, model, strict))
                break
            except urllib.error.HTTPError as e:
                if e.code == 400 and strict:   # an endpoint that refuses `strict`: the rest go without it
                    strict = False
                    out["problems"].append("the endpoint refused strict tool schemas (HTTP 400): sent without")
                    continue
                if e.code not in inflight.REFUSALS:
                    out["problems"].append(f"HTTP {e.code}")
                    break
                inflight.refused()
                waits.refusals += 1
                if k > RETRIES:
                    failed = "refused"
                    break
                wait = _jitter_wait(k, e.headers)
            except (urllib.error.URLError, OSError, ValueError) as e:
                out["problems"].append(type(e).__name__)
                break
            finally:
                slot.release()
            if wait:
                sleep(wait)   # the slot is free while it waits
        if data is None:
            out[failed] += 1
            continue
        parts = chatbridge.output_parts(data)
        hits = [k for k, r in rx.items() if any(r.search(t) for _, t in parts)]
        if hits:
            out["junk_responses"] += 1
            for h in hits:
                out["junk_hits"][h] = out["junk_hits"].get(h, 0) + 1
        calls = [c for ch in data.get("choices") or () if isinstance(ch, dict)
                 for c in ((ch.get("message") or {}).get("tool_calls") or ()) if isinstance(c, dict)]
        if not calls:
            out["no_tool_call"] += 1
            continue
        fn = calls[0].get("function") or {}
        schema = schemas().get(fn.get("name"))
        try:
            args = json.loads(fn.get("arguments") or "")
        except ValueError:
            args = None
        if fn.get("name") != tool:
            out["wrong_tool"] += 1
        if schema is None or not isinstance(args, dict):
            out["invalid"] += 1
            continue
        items.append([args, schema])
    errs, out["validator"] = validate(items, python)
    bad = sum(1 for e in errs if e)
    out["invalid"] += bad
    out["valid"] = len(items) - bad
    out["inflight"] = waits.export()
    out["problems"] = sorted(set(out["problems"]))
    return out


def endpoint_post(alias: str, base_url: str = ""):
    """A post(body) for the saved endpoint `alias` (its key and URL stay in here, never printed)."""
    got = endpoint.credentials(alias)
    if not got:
        raise ValueError(f"no key saved for endpoint {alias!r} (`ttp secret endpoint {alias} --url <base url>`)")
    key, saved = got
    url = endpoint.base_url(base_url or saved) + "/chat/completions"
    opener = urllib.request.build_opener(endpoint._NoRedirect)

    def post(body: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with opener.open(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read().decode())
            glmquota.answer(alias, r.headers, data)   # an off-bridge post: counted like a bridge's
            return data
    return post


def main(cfg: dict, n: int, alias: str = "", model: str = "", tier: str = "standard") -> dict:
    """The probe on the project's providers.chat entry (`cfg`: its config), or the given endpoint and model."""
    entry = ((cfg or {}).get("providers") or {}).get("chat") or {}
    alias = alias or str(entry.get("endpoint") or "")
    tiers = entry.get("tiers") or {}
    model = model or str((tiers.get(tier) or {}).get("model") or "")
    if not alias or not model:
        raise ValueError("no chat endpoint or model: set providers.chat (endpoint, tiers.<tier>.model) "
                         "or pass --endpoint and --model")
    jp = entry.get("junk_patterns")
    os.environ.setdefault(inflight.PRIORITY_ENV, "low")   # a probe never takes a slot before project work
    return probe(n, endpoint_post(alias, str(entry.get("base_url") or "")), model,
                 jp if isinstance(jp, dict) and not chatbridge._junk_problems(jp) else None)
