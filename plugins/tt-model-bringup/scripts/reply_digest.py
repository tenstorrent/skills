#!/usr/bin/env python3
"""Per-reply digest of a served model's own responses, read from the server's response log (agentic-serving).

Deterministic; it measures and flags, it never judges. Per reply: prompt and completion tokens, finish reason,
characters, and the zlib compression ratio and distinct-character count of the last and first TAIL_CHARS
characters. A reply of at least MIN_FLAG_CHARS characters is flagged when its tail compresses below
FLAG_ZLIB_RATIO or holds at most FLAG_DISTINCT_CHARS distinct characters (degenerate: collapse into a few
characters or a token loop). On the Gemma 4 31B v4 run these thresholds flagged 18 of 19 capped replies and
0 of 128 normal ones; the miss (a garbled phrase loop, ratio 0.15) overlaps the most repetitive normal reply
(0.139), so an unflagged capped reply goes to a triage sub-agent, never to the thresholds.

Replies map to eval tasks through the run's per-call accounting (`--accounting`, JSON that lists per task the
response ids of its model calls; see `task_map`). Output in `--out` (doc/agentic_evals/current/<run>/digest/):
one `<task>.json` per task (one line per model call, plus the last TAIL_KEEP_CHARS characters of each flagged or
capped reply) and `summary.json`; a summary table is printed.

`--check-log` is the logging check before the first eval run: the log must hold a complete response (body,
token counts, finish reason) for `--id` (or any response), and it is recorded with `--record`.

Logs: plain `response_body={...}` lines (vLLM with request and response logging on) and hex-framed records
(`TTNATIVEHEX1 <hex>` or `TTNATIVEHEX2 <sha256> <index> <count> <size> <hex>` of a JSON {name, message}).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import zlib
from pathlib import Path

TAIL_CHARS = 4096          # window measured at the end (and start) of each reply
FLAG_ZLIB_RATIO = 0.05     # compressed / raw bytes of the tail below this: flag
FLAG_DISTINCT_CHARS = 10   # at most this many distinct characters in the tail: flag
MIN_FLAG_CHARS = 1000      # shorter replies are never flagged (a short answer has few distinct characters)
TAIL_KEEP_CHARS = 2000     # tail kept in a task digest for each flagged or capped reply
MAX_TAILS = 12             # tails kept per task (first and last ones when there are more)
ACTION_CHARS = 160         # action summary per call
TASK_KEYS = ("task", "task_id", "instance_id", "doc_id")
MARKER = "response_body={"


def thresholds() -> dict:
    return {"tail_chars": TAIL_CHARS, "flag_zlib_ratio_below": FLAG_ZLIB_RATIO,
            "flag_distinct_chars_at_most": FLAG_DISTINCT_CHARS, "min_flag_chars": MIN_FLAG_CHARS}


def framed(lines):
    """Yield log messages: plain lines, and the `message` of hex-framed records (reassembled, checksummed)."""
    frames: dict = {}
    for line in lines:
        match = re.search(r"TTNATIVEHEX2 ([0-9a-f]{64}) (\d+) (\d+) (\d+) ([0-9a-f]+)", line)
        if match:
            digest, index, count, size, encoded = match.groups()
            part = unhex(encoded)
            if part is None:  # a record cut off mid-write (server restart): its checksum can never match
                continue
            state = frames.setdefault(digest, {"count": int(count), "size": int(size), "parts": {}})
            state["parts"][int(index)] = part
            if len(state["parts"]) == state["count"]:
                payload = b"".join(state["parts"].get(i, b"") for i in range(state["count"]))
                del frames[digest]
                if len(payload) == state["size"] and hashlib.sha256(payload).hexdigest() == digest:
                    yield message_of(payload)
            continue
        match = re.search(r"TTNATIVEHEX1 ([0-9a-f]+)", line)
        if match:
            payload = unhex(match[1])
            yield message_of(payload) if payload is not None else ""
        else:
            yield line


def unhex(encoded: str) -> bytes | None:
    """Hex payload, or None for a truncated (odd-length) record."""
    try:
        return bytes.fromhex(encoded)
    except ValueError:
        return None


def message_of(payload: bytes) -> str:
    try:
        record = json.loads(payload)
    except ValueError:
        return ""
    return record.get("message", "") if isinstance(record, dict) else ""


def responses(lines):
    """Complete chat-completion bodies in the log, in log order (streamed chunks have no choices.message)."""
    decoder = json.JSONDecoder()
    for message in framed(lines):
        start = message.find(MARKER)
        if start < 0:
            continue
        try:
            body, _ = decoder.raw_decode(message[start + len(MARKER):])
        except ValueError:
            continue
        if isinstance(body, dict) and body.get("id") and isinstance(body.get("choices"), list) and body["choices"]:
            yield body


def reply_text(body: dict) -> str:
    """Everything the model generated in the first choice: reasoning, content, tool-call arguments."""
    message = body["choices"][0].get("message") or {}
    text = "".join(v for k in ("reasoning_content", "reasoning", "content")
                   for v in [message.get(k)] if isinstance(v, str))
    for call in message.get("tool_calls") or []:
        function = (call or {}).get("function") or {}
        text += f"\n{function.get('name', '')}({function.get('arguments', '')})"
    return text


def window_stats(text: str) -> tuple[float | None, int]:
    if not text:
        return None, 0
    raw = text.encode()
    return round(len(zlib.compress(raw, 9)) / len(raw), 4), len(set(text))


def action(body: dict) -> str:
    """A short summary of what the reply asked for: its command or tool call, else the end of its content."""
    message = body["choices"][0].get("message") or {}
    calls = message.get("tool_calls") or []
    if calls:
        function = (calls[0] or {}).get("function") or {}
        return f"tool {function.get('name', '')} {function.get('arguments', '')}"[:ACTION_CHARS]
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    blocks = re.findall(r"```(?:bash|sh|shell|console|mswea_bash_command)\s*\n(.*?)```", content, re.S)
    if blocks:
        return "cmd: " + " ".join(blocks[-1].split())[:ACTION_CHARS]
    tail = " ".join(content.split())[-80:]
    return f"text: ...{tail}" if tail else "(no content)"


def reply_row(body: dict, cap: int | None = None) -> dict:
    text = reply_text(body)
    usage = body.get("usage") or {}
    tail_ratio, tail_distinct = window_stats(text[-TAIL_CHARS:])
    head_ratio, head_distinct = window_stats(text[:TAIL_CHARS])
    finish = body["choices"][0].get("finish_reason")
    flag = len(text) >= MIN_FLAG_CHARS and (tail_ratio < FLAG_ZLIB_RATIO or tail_distinct <= FLAG_DISTINCT_CHARS)
    return {"id": body["id"], "prompt_tokens": usage.get("prompt_tokens"), "tokens": usage.get("completion_tokens"),
            "finish": finish, "capped": finish == "length" or bool(cap and (usage.get("completion_tokens") or 0) >= cap),
            "chars": len(text),
            "tail_ratio": tail_ratio, "tail_distinct": tail_distinct,
            "head_ratio": head_ratio, "head_distinct": head_distinct, "flag": flag,
            "action": action(body), "_tail": text[-TAIL_KEEP_CHARS:]}


def task_map(accounting) -> dict:
    """{task: [response ids]} from per-call accounting JSON: any object with a task key (task, task_id,
    instance_id, doc_id) and its call ids (`calls[].id`, `response_id`, `response_ids`, `call_ids`). A task with
    no resolved ids is kept with an empty list. `{"tasks": {"<task>": ["<id>", ...]}}` also works."""
    tasks: dict = {}

    def add(task, ids):
        bucket = tasks.setdefault(str(task), [])
        bucket.extend(i for i in ids if isinstance(i, str) and i and i not in bucket)

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if isinstance(node.get("tasks"), dict) and all(isinstance(v, list) for v in node["tasks"].values()):
            for task, ids in node["tasks"].items():
                add(task, ids)
            return
        key = next((k for k in TASK_KEYS if isinstance(node.get(k), (str, int)) and not isinstance(node.get(k), bool)), None)
        calls = node.get("calls")
        if key and (isinstance(calls, list) or any(k in node for k in ("response_id", "response_ids", "call_ids"))):
            ids = [c.get("id") for c in calls or [] if isinstance(c, dict)]
            ids += [node.get("response_id")] + list(node.get("response_ids") or []) + list(node.get("call_ids") or [])
            add(node[key], ids)
            return
        for value in node.values():
            walk(value)

    walk(accounting)
    return tasks


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "task"


def task_digest(task: str, rows: list, run: str) -> dict:
    calls, seen, tails = [], {}, []
    for n, row in enumerate(rows, 1):
        same = seen.setdefault(row["action"], n) if row["action"].startswith(("cmd:", "tool ")) else n
        line = {k: v for k, v in row.items() if not k.startswith("_")}
        line["n"] = n
        if same != n:
            line["same_as"] = same
        calls.append(line)
        if row["flag"] or row["capped"]:
            tails.append({"n": n, "id": row["id"], "tail": row["_tail"]})
    if len(tails) > MAX_TAILS:
        tails = tails[:MAX_TAILS // 2] + tails[-MAX_TAILS // 2:]
    digest = {"task": task, "run": run, "thresholds": thresholds(), "calls": calls, "tails": tails,
              "capped": sum(r["capped"] for r in rows), "flagged": sum(r["flag"] for r in rows)}
    digest["approx_tokens"] = len(json.dumps(digest)) // 4
    return digest


def dump_digest(digest: dict) -> str:
    """JSON with one line per model call and per kept tail."""
    head = {k: v for k, v in digest.items() if k not in ("calls", "tails")}
    parts = [json.dumps(head)[:-1]]
    for key in ("calls", "tails"):
        items = ",\n  ".join(json.dumps(item) for item in digest[key])
        parts.append(f', "{key}": [\n  {items}\n]' if items else f', "{key}": []')
    return "".join(parts) + "}\n"


def summary_row(task: str, rows: list) -> dict:
    ratios = [r["tail_ratio"] for r in rows if r["tail_ratio"] is not None and r["chars"] >= MIN_FLAG_CHARS]
    return {"task": task, "calls": len(rows), "capped": sum(r["capped"] for r in rows),
            "flagged": sum(r["flag"] for r in rows), "flagged_ids": [r["id"] for r in rows if r["flag"]],
            "max_tokens": max((r["tokens"] or 0 for r in rows), default=0),
            "min_tail_ratio": min(ratios, default=None),
            "min_tail_distinct": min((r["tail_distinct"] for r in rows if r["chars"] >= MIN_FLAG_CHARS), default=None),
            "digest": f"{safe(task)}.json"}


def table(summary: dict) -> str:
    out = ["| task | calls | capped | flagged | max out tokens | min tail ratio | min tail distinct |",
           "|---|---|---|---|---|---|---|"]
    for row in summary["tasks"].values():
        out.append(f"| {row['task']} | {row['calls']} | {row['capped']} | {row['flagged']} | {row['max_tokens']} | "
                   f"{row['min_tail_ratio']} | {row['min_tail_distinct']} |")
    un = summary["unmapped"]
    out.append(f"| (unmapped) | {un['replies']} | {un['capped']} | {un['flagged']} | | | |")
    return "\n".join(out)


def read_replies(logs: list, cap: int | None = None) -> dict:
    rows: dict = {}
    for log in logs:
        with open(log, errors="replace") as source:
            for body in responses(source):
                rows.setdefault(body["id"], reply_row(body, cap))
    return rows


def digest_run(logs: list, accounting: list, out: Path, run: str, extra_tasks: list, cap: int | None = None) -> dict:
    replies = read_replies(logs, cap)
    mapping: dict = {}
    for path in accounting:
        for task, ids in task_map(json.loads(Path(path).read_text())).items():
            mapping.setdefault(task, [])
            mapping[task] += [i for i in ids if i not in mapping[task]]
    for task in extra_tasks:
        mapping.setdefault(task, [])
    out.mkdir(parents=True, exist_ok=True)
    summary = {"run": run, "logs": [str(p) for p in logs], "thresholds": thresholds(), "replies": len(replies),
               "tasks": {}, "missing_ids": {}}
    mapped = set()
    for task, ids in mapping.items():
        rows = [replies[i] for i in ids if i in replies]
        missing = [i for i in ids if i not in replies]
        if missing:
            summary["missing_ids"][task] = missing
        mapped.update(r["id"] for r in rows)
        (out / f"{safe(task)}.json").write_text(dump_digest(task_digest(task, rows, run)))
        summary["tasks"][task] = summary_row(task, rows)
    rest = [r for i, r in replies.items() if i not in mapped]
    summary["unmapped"] = {"replies": len(rest), "capped": sum(r["capped"] for r in rest),
                           "flagged": sum(r["flag"] for r in rest),
                           "capped_or_flagged": [{k: v for k, v in r.items() if not k.startswith("_") and k != "action"}
                                                 for r in rest if r["capped"] or r["flag"]]}
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    return summary


def check_log(logs: list, response_id: str | None, record: Path | None) -> int:
    rows = read_replies(logs)
    row = rows.get(response_id) if response_id else (list(rows.values())[-1] if rows else None)
    problems = []
    if row is None:
        problems.append(f"no complete response {'for ' + response_id if response_id else ''} in the log; turn on "
                        "response-body logging on the served route")
    else:
        if not row["chars"]:
            problems.append("the logged response has no body text")
        if not (isinstance(row["prompt_tokens"], int) and isinstance(row["tokens"], int)):
            problems.append("the logged response has no token counts (usage)")
        if not row["finish"]:
            problems.append("the logged response has no finish reason")
    result = {"pass": not problems, "problems": problems, "logs": [str(p) for p in logs],
              "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "thresholds": thresholds()}
    if row:
        result.update({k: row[k] for k in ("id", "prompt_tokens", "tokens", "finish", "chars")})
    if record:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    return 0 if result["pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", action="append", required=True, help="server log with response bodies (repeatable)")
    parser.add_argument("--accounting", action="append", default=[], help="per-call accounting JSON (repeatable)")
    parser.add_argument("--task", action="append", default=[], help="a task to digest even with no mapped calls")
    parser.add_argument("--out", help="digest directory, doc/agentic_evals/current/<run>/digest")
    parser.add_argument("--output-cap", type=int, help="a reply with this many completion tokens counts as capped "
                        "whatever its finish reason (some tool parsers report tool_calls at the cap)")
    parser.add_argument("--run", default="", help="run id recorded in the digests")
    parser.add_argument("--check-log", action="store_true", help="logging check: one complete logged response")
    parser.add_argument("--id", help="response id of the logging-check request")
    parser.add_argument("--record", type=Path, help="write the logging check here (doc/agentic_evals/logging_check.json)")
    args = parser.parse_args()
    if args.check_log:
        return check_log(args.log, args.id, args.record)
    if not args.out:
        parser.error("--out is required unless --check-log")
    summary = digest_run(args.log, args.accounting, Path(args.out), args.run or Path(args.out).parent.name,
                         args.task, args.output_cap)
    print(table(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
