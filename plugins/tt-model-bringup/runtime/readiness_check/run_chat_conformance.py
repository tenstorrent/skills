# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""P3 eval-readiness probe: chat, tools and thinking conformance.

Sends about a dozen fixed requests through ``/v1/chat/completions`` of an
already-running vLLM server, shaped like the ones lm-eval and agent harnesses
(Harbor terminus-2, tool-calling evals) send:

  c01 system message with list-shaped content parts
  c02 thinking on: answer in ``content``, thinking in ``reasoning_content``
  c03 thinking off (when the chat template has a switch)
  c04 tool call with a JSON-looking string argument and nested object arguments
  c05 tool-result turn, then an answer
  c06 multi-turn history whose assistant turns carry NO reasoning field
  c07 multi-turn history with ``reasoning_content`` on assistant turns
  c08 continue a partial assistant message (continue_final_message)
  c09 long system prompt
  c10 n=1 with stop strings
  c11 streamed thinking-on request
  c12 JSON mode (response_format)

It also compares the server's rendered prompt (``/tokenize``) with the HF
``apply_chat_template`` for c01, c06 and c07 when both are available.

Checks: a refused request is critical (not waivable for c01 and c02); an
empty required field, thinking tags in ``content`` or a tool argument of the
wrong type is critical; optional features (c03, c12) and answer-quality hints
are advisory.

    python -m readiness_check.run_chat_conformance \\
        --server-url http://localhost:8000 \\
        --model-dir models/autoports/<model> --hf-model <hf-model-id> \\
        --server-log <server.log>

Result: ``<model_dir>/doc/eval_readiness/chat_conformance.json``; requests and
responses under ``doc/eval_readiness/chat_conformance/``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from readiness_check.eval_readiness_result import RESULT_DIR, check, print_summary, write_result
from readiness_check.served_api import (
    THINKING_TAGS,
    delta_text,
    error_text,
    http_json,
    parse_server_settings,
    read_text,
    resolve_served_model,
    stream_post,
)

PROBE = "chat_conformance"
NOT_WAIVABLE = ("c01", "c02")
DEFAULT_MAX_TOKENS = 8192
DEFAULT_SHORT_MAX_TOKENS = 512
DEFAULT_LONG_SYSTEM_TOKENS = 4096
THINKING_HINT = re.compile(r"enable_thinking|reasoning_content|<think>|thinking|reasoning_effort")
CODE_WORD = "PELICAN-4471"
FILLER = (
    "The operations manual covers routine maintenance of the warehouse conveyors. Belts are inspected "
    "every morning for wear, rollers are lubricated weekly, and any unusual noise is logged with the "
    "time, the belt number and the name of the person who heard it. "
)

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write text to a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
                "content": {"type": "string", "description": "Exact text to write, verbatim."},
                "options": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["w", "a"]},
                        "overwrite": {"type": "boolean"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["mode", "overwrite", "tags"],
                },
            },
            "required": ["path", "content", "options"],
        },
    },
}
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


class Ctx:
    """What the cases need to know about the model and server."""

    def __init__(self, args: argparse.Namespace, model: str, template: Optional[str], settings: dict, tags: list[str]):
        self.args = args
        self.model = model
        self.template = template or ""
        self.settings = settings
        self.tags = tags
        if args.thinking == "auto":
            self.thinks = bool(settings.get("reasoning_parser")) or bool(THINKING_HINT.search(self.template))
        else:
            self.thinks = args.thinking == "on"
        has_switch = "enable_thinking" in self.template
        self.on_kwargs = _json_arg(args.thinking_on_kwargs) if args.thinking_on_kwargs else (
            {"enable_thinking": True} if has_switch else {})
        self.off_kwargs = _json_arg(args.thinking_off_kwargs) if args.thinking_off_kwargs else (
            {"enable_thinking": False} if has_switch else None)
        self.tools = args.tools == "on" or (args.tools == "auto" and "tools" in self.template)
        self.sampling = _json_arg(args.sampling)

    def quiet_kwargs(self) -> dict:
        """Thinking off when the template can switch it off, else the template default."""
        return self.off_kwargs if self.off_kwargs is not None else {}


def _json_arg(text: str) -> dict:
    value = json.loads(text) if text else {}
    if not isinstance(value, dict):
        raise SystemExit(f"expected a JSON object, got {text!r}")
    return value


def _message(body: Any) -> dict:
    try:
        return body["choices"][0]["message"] or {}
    except (KeyError, IndexError, TypeError):
        return {}


def _finish(body: Any) -> Optional[str]:
    try:
        return body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None


def _reasoning(msg: dict) -> str:
    return msg.get("reasoning_content") or msg.get("reasoning") or ""


def _leaks(text: str, tags: list[str]) -> list[str]:
    return [t for t in tags if t and t in (text or "")]


def _answer_checks(ctx: Ctx, body: Any, *, need_reasoning: bool, expect: Optional[str] = None) -> tuple[list, list]:
    """Hard and soft problems for a plain answer."""
    hard, soft = [], []
    msg = _message(body)
    content, reasoning = msg.get("content") or "", _reasoning(msg)
    if not content.strip():
        if _finish(body) == "length":
            # The model was still thinking at the probe's budget: a probe budget limit,
            # not a serving defect. Report it, and rerun with a larger --max-tokens.
            soft.append("empty content at the token budget (finish_reason length: raise --max-tokens)")
        else:
            hard.append("empty content")
    if need_reasoning and not reasoning.strip():
        hard.append("empty reasoning_content/reasoning (is --reasoning-parser set for this family?)")
    leaked = _leaks(content, ctx.tags)
    if leaked:
        hard.append(f"thinking tags in content: {leaked}")
    if expect and content.strip() and expect.lower() not in content.lower():
        soft.append(f"answer does not contain {expect!r}: {content.strip()[:80]!r}")
    return hard, soft


# --- the cases --------------------------------------------------------------

def c01(ctx: Ctx) -> dict:
    return {
        "payload": {
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": "You are a concise assistant. Answer in English."}]},
                {"role": "user", "content": [{"type": "text", "text": "What is the capital of France? Answer in one word."}]},
            ],
            "chat_template_kwargs": ctx.quiet_kwargs(),
        },
        "validate": lambda b: _answer_checks(ctx, b, need_reasoning=False, expect="Paris"),
        "render": True,
    }


def c02(ctx: Ctx) -> Optional[dict]:
    if not ctx.thinks:
        return None
    return {
        "payload": {
            "messages": [{"role": "user", "content": "What is 17 + 25? Think it through, then give only the number."}],
            "chat_template_kwargs": ctx.on_kwargs,
            "max_tokens": ctx.args.max_tokens,
        },
        "validate": lambda b: _answer_checks(ctx, b, need_reasoning=True, expect="42"),
    }


def c03(ctx: Ctx) -> Optional[dict]:
    if not ctx.thinks or ctx.off_kwargs is None:
        return None

    def validate(b):
        hard, soft = _answer_checks(ctx, b, need_reasoning=False, expect="42")
        if _reasoning(_message(b)).strip():
            soft.append("reasoning returned with thinking off")
        return hard, soft

    return {
        "payload": {
            "messages": [{"role": "user", "content": "What is 17 + 25? Give only the number."}],
            "chat_template_kwargs": ctx.off_kwargs,
        },
        "validate": validate,
    }


def _tool_arg_problems(args: dict) -> list[str]:
    problems = []
    if not isinstance(args.get("path"), str):
        problems.append(f"path is {type(args.get('path')).__name__}, schema says string")
    if not isinstance(args.get("content"), str):
        problems.append(
            f"content is {type(args.get('content')).__name__}, schema says string "
            "(the parser decoded a JSON-looking string; use a schema-aware tool parser)")
    options = args.get("options")
    if not isinstance(options, dict):
        problems.append(f"options is {type(options).__name__}, schema says object")
        return problems
    if not isinstance(options.get("mode"), str):
        problems.append("options.mode is not a string")
    if not isinstance(options.get("overwrite"), bool):
        problems.append(f"options.overwrite is {type(options.get('overwrite')).__name__}, schema says boolean")
    tags = options.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        problems.append("options.tags is not an array of strings")
    return problems


def c04(ctx: Ctx) -> Optional[dict]:
    if not ctx.tools:
        return None

    def validate(b):
        hard, soft = [], []
        msg = _message(b)
        calls = msg.get("tool_calls") or []
        if not calls:
            content = msg.get("content") or ""
            hint = " (tool-call markup left in content: the tool parser did not extract it)" if (
                "write_file" in content) else ""
            return [f"no tool_calls parsed{hint}; check --enable-auto-tool-choice and --tool-call-parser"], soft
        fn = calls[0].get("function") or {}
        if fn.get("name") != "write_file":
            hard.append(f"called {fn.get('name')!r}, expected write_file")
        raw = fn.get("arguments")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            hard.append(f"arguments are not a JSON object string: {str(raw)[:80]!r}")
        else:
            hard += _tool_arg_problems(parsed)
        hard += [f"thinking tags in content: {t}" for t in _leaks(msg.get("content") or "", ctx.tags)]
        return hard, soft

    return {
        "payload": {
            "messages": [{"role": "user", "content": (
                "Use the write_file tool now. Path: config.json. Content: this exact JSON text, as a string: "
                '{"name": "demo", "values": [1, 2, 3]} . Options: mode "w", overwrite true, tags ["a", "b"].')}],
            "tools": [WRITE_FILE_TOOL],
            "tool_choice": "auto",
            "chat_template_kwargs": ctx.quiet_kwargs(),
            "max_tokens": ctx.args.max_tokens,
        },
        "validate": validate,
    }


def _assistant(ctx: Ctx, content: str, reasoning: Optional[str], **extra) -> dict:
    msg = {"role": "assistant", "content": content, **extra}
    if reasoning is not None and ctx.thinks:
        msg["reasoning_content"] = reasoning
    return msg


def c05(ctx: Ctx) -> Optional[dict]:
    if not ctx.tools:
        return None
    call = {"id": "call_0001", "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": "Paris"})}}
    return {
        "payload": {
            "messages": [
                {"role": "user", "content": "What is the weather in Paris right now?"},
                _assistant(ctx, "", "The user wants the weather; call get_weather.", tool_calls=[call]),
                {"role": "tool", "tool_call_id": "call_0001", "content": json.dumps({"temperature_c": 18, "sky": "sunny"})},
            ],
            "tools": [WEATHER_TOOL],
            "chat_template_kwargs": ctx.quiet_kwargs(),
            "max_tokens": ctx.args.max_tokens,
        },
        "validate": lambda b: _answer_checks(ctx, b, need_reasoning=False, expect="18"),
    }


def _history(ctx: Ctx, with_reasoning: bool) -> list[dict]:
    r1 = "The user introduced themselves." if with_reasoning else None
    r2 = "2 + 3 = 5." if with_reasoning else None
    first = {"role": "assistant", "content": "Nice to meet you, Ada."}
    second = {"role": "assistant", "content": "5"}
    if with_reasoning:
        first, second = _assistant(ctx, first["content"], r1), _assistant(ctx, second["content"], r2)
    return [
        {"role": "user", "content": "Hello, my name is Ada."},
        first,
        {"role": "user", "content": "What is 2 + 3?"},
        second,
        {"role": "user", "content": "What is my name? Answer with one word."},
    ]


def c06(ctx: Ctx) -> dict:
    return {
        "payload": {"messages": _history(ctx, with_reasoning=False), "chat_template_kwargs": ctx.quiet_kwargs(),
                    "max_tokens": ctx.args.max_tokens},
        "validate": lambda b: _answer_checks(ctx, b, need_reasoning=False, expect="Ada"),
        "render": True,
    }


def c07(ctx: Ctx) -> Optional[dict]:
    if not ctx.thinks:
        return None
    return {
        "payload": {"messages": _history(ctx, with_reasoning=True), "chat_template_kwargs": ctx.quiet_kwargs(),
                    "max_tokens": ctx.args.max_tokens},
        "validate": lambda b: _answer_checks(ctx, b, need_reasoning=False, expect="Ada"),
        "render": True,
    }


def c08(ctx: Ctx) -> dict:
    def validate(b):
        msg = _message(b)
        text = (msg.get("content") or "") + _reasoning(msg)
        hard = [] if text.strip() else ["empty continuation"]
        hard += [f"thinking tags in content: {t}" for t in _leaks(msg.get("content") or "", ctx.tags)]
        return hard, []

    return {
        "payload": {
            "messages": [
                {"role": "user", "content": "Write one sentence about the sea."},
                _assistant(ctx, "The sea is", "One sentence about the sea."),
            ],
            "continue_final_message": True,
            "add_generation_prompt": False,
            "chat_template_kwargs": ctx.quiet_kwargs(),
        },
        "validate": validate,
    }


def c09(ctx: Ctx) -> dict:
    target_chars = ctx.args.long_system_tokens * 4
    filler = (FILLER * (target_chars // len(FILLER) + 1))[:target_chars]
    system = f"Remember this code word: {CODE_WORD}. " + filler
    return {
        "payload": {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": "What is the code word from the system message? Reply with it only."},
            ],
            "chat_template_kwargs": ctx.quiet_kwargs(),
            "max_tokens": ctx.args.max_tokens,
        },
        "validate": lambda b: _answer_checks(ctx, b, need_reasoning=False, expect="PELICAN"),
    }


def c10(ctx: Ctx) -> dict:
    stops = ["15", "DONE"]

    def validate(b):
        msg = _message(b)
        content = msg.get("content") or ""
        hard = [f"stop string {s!r} in content" for s in stops if s in content]
        hard += [f"thinking tags in content: {t}" for t in _leaks(content, ctx.tags)]
        soft = [] if _finish(b) == "stop" else [f"finish_reason {_finish(b)!r}, expected 'stop'"]
        if not content.strip():
            soft.append("empty content (a stop string may have ended the output while thinking)")
        return hard, soft

    return {
        "payload": {
            "messages": [{"role": "user", "content": "Count from 1 to 30, separated by commas, then write DONE."}],
            "n": 1,
            "stop": stops,
            "chat_template_kwargs": ctx.quiet_kwargs(),
        },
        "validate": validate,
    }


def c11(ctx: Ctx) -> Optional[dict]:
    if not ctx.thinks:
        return None
    case = c02(ctx)
    case["stream"] = True
    return case


def c12(ctx: Ctx) -> dict:
    def validate(b):
        content = (_message(b).get("content") or "").strip()
        try:
            ok = isinstance(json.loads(content), dict)
        except json.JSONDecodeError:
            ok = False
        return [], ([] if ok else [f"content is not a JSON object: {content[:80]!r}"])

    return {
        "payload": {
            "messages": [{"role": "user", "content": "Give a JSON object with keys city and country for Paris."}],
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": ctx.quiet_kwargs(),
        },
        "validate": validate,
        "optional": True,
    }


CASES: list[tuple[str, str, Callable[[Ctx], Optional[dict]], str]] = [
    ("c01", "system_list_content", c01, ""),
    ("c02", "thinking_split", c02, "the model has no thinking mode (no reasoning parser, no thinking switch in the template)"),
    ("c03", "thinking_off", c03, "the chat template has no thinking-off switch (pass --thinking-off-kwargs if it has one)"),
    ("c04", "tool_call_types", c04, "the chat template takes no tools"),
    ("c05", "tool_result_turn", c05, "the chat template takes no tools"),
    ("c06", "history_without_reasoning", c06, ""),
    ("c07", "history_with_reasoning", c07, "the model has no thinking mode"),
    ("c08", "continue_final_message", c08, ""),
    ("c09", "long_system_prompt", c09, ""),
    ("c10", "stop_strings", c10, ""),
    ("c11", "thinking_stream", c11, "the model has no thinking mode"),
    ("c12", "json_mode", c12, ""),
]
OPTIONAL_CASES = ("c03", "c12")


def _send(ctx: Ctx, case: dict) -> tuple[int, Any]:
    url = f"{ctx.args.server_url.rstrip('/')}/v1/chat/completions"
    # A thinking model without an off switch thinks on every case, so give it the long budget.
    budget = ctx.args.short_max_tokens if (not ctx.thinks or ctx.off_kwargs is not None) else ctx.args.max_tokens
    payload = {"model": ctx.model, "max_tokens": budget, **ctx.sampling, **case["payload"]}
    if not payload.get("chat_template_kwargs"):
        payload.pop("chat_template_kwargs", None)
    if not case.get("stream"):
        status, body, _ = http_json("POST", url, payload, timeout=ctx.args.timeout)
        return status, body
    result = stream_post(url, payload, timeout=ctx.args.timeout)
    if result["status"] != 200 or result.get("error"):
        return result["status"] or 0, {"error": {"message": result.get("error")}}
    content, reasoning, finish = "", "", None
    for _, chunk in result["chunks"]:
        c, r = delta_text(chunk)
        content, reasoning = content + c, reasoning + r
        for choice in chunk.get("choices") or []:
            if isinstance(choice, dict):
                finish = choice.get("finish_reason") or finish
    return 200, {"choices": [{"message": {"role": "assistant", "content": content, "reasoning_content": reasoning},
                              "finish_reason": finish}]}


def _flatten(messages: list[dict]) -> list[dict]:
    """HF-side messages: list content parts joined into a string, as vLLM's 'string' content format does."""
    out = []
    for m in messages:
        m = dict(m)
        if isinstance(m.get("content"), list):
            m["content"] = "\n".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
        out.append(m)
    return out


def _render_compare(ctx: Ctx, tokenizer: Any, case_id: str, payload: dict) -> dict:
    """Compare /tokenize of the chat request with HF apply_chat_template. Returns a check row."""
    cid = f"render_{case_id}"
    kwargs = payload.get("chat_template_kwargs") or {}
    status, body, _ = http_json("POST", f"{ctx.args.server_url.rstrip('/')}/tokenize", {
        "model": ctx.model, "messages": payload["messages"], "add_generation_prompt": True,
        **({"chat_template_kwargs": kwargs} if kwargs else {}),
    }, timeout=120)
    if status == 404:
        return check(cid, None, severity="advisory", detail="server has no /tokenize endpoint")
    if status != 200 or not isinstance(body, dict) or "tokens" not in body:
        return check(cid, None, severity="advisory", detail=f"/tokenize returned {status}: {error_text(body)}")
    if tokenizer is None:
        return check(cid, None, severity="advisory", detail="HF tokenizer not loadable here")
    try:
        text = tokenizer.apply_chat_template(_flatten(payload["messages"]), tokenize=False,
                                             add_generation_prompt=True, **kwargs)
        hf_ids = list(tokenizer(text, add_special_tokens=False)["input_ids"])
    except Exception as exc:  # noqa: BLE001 - template errors are the finding
        return check(cid, None, severity="advisory",
                     detail=f"HF apply_chat_template raises ({type(exc).__name__}: {str(exc)[:120]}); "
                            "the original model shares this, record it with original_model 'same'")
    served = list(body["tokens"])
    if served == hf_ids:
        return check(cid, True, severity="advisory", value=len(served))
    pos = next((i for i, (a, b) in enumerate(zip(served, hf_ids)) if a != b), min(len(served), len(hf_ids)))
    served_tail = tokenizer.decode(served[pos:pos + 12]) if hasattr(tokenizer, "decode") else served[pos:pos + 12]
    hf_tail = tokenizer.decode(hf_ids[pos:pos + 12]) if hasattr(tokenizer, "decode") else hf_ids[pos:pos + 12]
    return check(cid, False, severity="advisory", value=pos, detail=(
        f"server prompt differs from HF apply_chat_template at token {pos} ({len(served)} vs {len(hf_ids)} tokens): "
        f"server {served_tail!r} vs HF {hf_tail!r}; check --chat-template-content-format and --chat-template"))


def _load_tokenizer(args: argparse.Namespace) -> tuple[Any, Optional[str]]:
    """HF tokenizer and its chat template text; either may be None."""
    template = None
    tokenizer = None
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(args.hf_model, revision=args.revision,
                                                  trust_remote_code=args.trust_remote_code)
        template = getattr(tokenizer, "chat_template", None)
    except Exception:  # noqa: BLE001 - fall back to reading template files
        tokenizer = None
    if isinstance(template, dict):
        template = "\n".join(str(v) for v in template.values())
    if not template:
        local = Path(args.hf_model)
        for name in ("chat_template.jinja", "tokenizer_config.json"):
            text = read_text(local / name)
            if text:
                template = text if name.endswith(".jinja") else str(json.loads(text).get("chat_template") or "")
                break
    return tokenizer, template


def _thinking_tags(tokenizer: Any) -> list[str]:
    tags = list(THINKING_TAGS)
    try:
        for token in (tokenizer.get_added_vocab() or {}):
            if re.search(r"think|reason|thought", token, re.I) and token not in tags:
                tags.append(token)
    except Exception:  # noqa: BLE001 - optional
        pass
    return tags


def _truncate(body: Any, limit: int = 2000) -> Any:
    text = json.dumps(body)
    return body if len(text) <= limit else text[:limit] + "...(truncated)"


def run(args: argparse.Namespace) -> int:
    model_dir = Path(args.model_dir)
    raw_dir = model_dir / RESULT_DIR / PROBE
    raw_dir.mkdir(parents=True, exist_ok=True)
    model, _ = resolve_served_model(args.server_url, args.served_model)
    tokenizer, template = _load_tokenizer(args)
    settings = parse_server_settings(read_text(args.server_log) + "\n" + (args.server_command or ""))
    ctx = Ctx(args, model, template, settings, _thinking_tags(tokenizer) if tokenizer else list(THINKING_TAGS))

    checks: list[dict] = []
    summary = [
        f"model {model}; thinking {'yes' if ctx.thinks else 'no'} (on {ctx.on_kwargs}, off {ctx.off_kwargs}); "
        f"tools {'yes' if ctx.tools else 'no'}",
        "server: " + ", ".join(f"{k}={settings[k]}" for k in (
            "reasoning_parser", "tool_call_parser", "tool_parser_plugin", "chat_template_content_format")
            if k in settings) if settings else "server settings unknown (pass --server-log or --server-command)",
    ]
    if settings:
        if ctx.thinks and not settings.get("reasoning_parser"):
            checks.append(check("reasoning_parser_set", False, severity="advisory", detail=(
                "thinking model served without --reasoning-parser; pick vLLM's parser for the family")))
        if ctx.tools and not (settings.get("tool_call_parser") and settings.get("enable_auto_tool_choice")):
            checks.append(check("tool_parser_set", False, severity="advisory", detail=(
                "tool-capable template served without --enable-auto-tool-choice and --tool-call-parser")))

    raw: dict[str, Any] = {"model": model, "settings": settings, "thinks": ctx.thinks, "tools": ctx.tools, "cases": {}}
    selected = {c for c in args.cases.split(",") if c} if args.cases else None
    for case_id, label, builder, skip_reason in CASES:
        if selected and case_id not in selected:
            continue
        cid = f"{case_id}_{label}"
        optional = case_id in OPTIONAL_CASES
        severity = "advisory" if optional else "critical"
        case = builder(ctx)
        if case is None:
            checks.append(check(cid, None, severity=severity, detail=skip_reason))
            continue
        try:
            status, body = _send(ctx, case)
        except Exception as exc:  # noqa: BLE001 - one broken request must not lose the whole result
            status, body = 0, {"error": {"message": f"probe error: {type(exc).__name__}: {exc}"}}
        raw["cases"][cid] = {"request": _truncate(case["payload"]), "status": status, "response": _truncate(body)}
        if status != 200:
            checks.append(check(cid, False, severity=severity, waivable=case_id not in NOT_WAIVABLE, detail=(
                f"refused: HTTP {status}: {error_text(body, 200)}")))
            summary.append(f"{case_id} {label}: REFUSED {status}")
        else:
            try:
                hard, soft = case["validate"](body)
            except Exception as exc:  # noqa: BLE001 - malformed response shape is a finding
                hard, soft = [f"response could not be validated: {type(exc).__name__}: {exc}"], []
            if optional:
                soft, hard = hard + soft, []
            checks.append(check(cid, not hard, severity=severity, waivable=case_id not in NOT_WAIVABLE,
                                detail="; ".join(hard)))
            if soft:
                checks.append(check(f"{cid}_quality", False, severity="advisory", detail="; ".join(soft)))
            summary.append(f"{case_id} {label}: {'ok' if not hard else 'FAIL'}{' (advisory notes)' if soft else ''}")
        if case.get("render") and not args.no_render_compare:
            checks.append(_render_compare(ctx, tokenizer, case_id, case["payload"]))

    raw_path = raw_dir / "responses.json"
    raw_path.write_text(json.dumps(raw, indent=2) + "\n")
    data = {"model": model, "thinks": ctx.thinks, "tools": ctx.tools, "settings": settings, "raw": str(raw_path)}
    path = write_result(model_dir, PROBE, checks, summary_lines=summary, data=data, output=args.output)
    return print_summary(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--server-url", default="http://localhost:8000")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--hf-model", required=True, help="HF id or local checkpoint dir (tokenizer, chat template)")
    parser.add_argument("--revision", default=None)
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--served-model", default=None)
    parser.add_argument("--server-log", type=Path, default=None, help="server log, to read parser settings")
    parser.add_argument("--server-command", default="", help="server command line, to read parser settings")
    parser.add_argument("--thinking", choices=("auto", "on", "off"), default="auto",
                        help="does the model think; auto reads the server's reasoning parser and the chat template")
    parser.add_argument("--thinking-on-kwargs", default="", help="chat_template_kwargs JSON for thinking on")
    parser.add_argument("--thinking-off-kwargs", default="", help="chat_template_kwargs JSON for thinking off")
    parser.add_argument("--tools", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--sampling", default='{"temperature": 0.0}',
                        help="sampling fields added to every request (default greedy)")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="budget for thinking/tool cases")
    parser.add_argument("--short-max-tokens", type=int, default=DEFAULT_SHORT_MAX_TOKENS)
    parser.add_argument("--long-system-tokens", type=int, default=DEFAULT_LONG_SYSTEM_TOKENS)
    parser.add_argument("--cases", default="", help="comma list of case ids (c01..c12); default all")
    parser.add_argument("--no-render-compare", action="store_true")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--output", default=None)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"ERROR {PROBE}: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
