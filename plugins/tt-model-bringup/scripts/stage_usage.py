#!/usr/bin/env python3
"""Per-stage Codex token and time usage, counted from a CODEX_HOME's rollout files.

The multigoal runner writes it (``## Stage usage`` in the stage's main report plus
``<stage doc dir>/stage_usage.json``); the stage check scripts validate it with ``check``.

Counting rule: every session under ``<CODEX_HOME>/sessions/**/rollout-*.jsonl`` with a
``token_count`` event whose timestamp is inside [start, end] is included (main thread,
sub-agents at any depth, any other thread in that home), selected by event timestamps, not
file names or mtimes. Each session contributes the sum of ``last_token_usage`` of its events
inside the window. Codex re-emits a ``token_count`` event with an unchanged
``total_token_usage`` (and the previous ``last_token_usage``) on rate-limit updates; such
repeats are not new usage and are skipped. ``total_token_usage`` is never differenced across
sessions.

Exit codes of ``check``: 0 pass (or not yet written by the runner), 2 missing or malformed,
3 usage error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
from typing import Any, Iterable

RUNNER_ORIGINATOR = "tt-metal-multigoal"
FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
ROW_KEYS = ("main_thread", "sub_agents", "other_threads", "total")
ROW_LABELS = {
    "main_thread": "main thread",
    "sub_agents": "sub-agents",
    "other_threads": "other threads",
    "total": "total",
}
HEADING = "## Stage usage"
HOME_LINE = (
    "Counted from the Codex sessions in {home} during the stage window; other homes on the "
    "same account are not included."
)
# Stage key (prompt stem without its "NN-" prefix) -> the stage's main report, relative to
# the model directory, and its doc directory (where stage_usage.json goes).
STAGE_REPORTS = {
    "functional-decoder": ("doc/functional_decoder/README.md", "doc/functional_decoder"),
    "fused-decoder": ("doc/fused_decoder/README.md", "doc/fused_decoder"),
    "optimized-decoder": ("doc/optimized_decoder/README.md", "doc/optimized_decoder"),
    "multichip-decoder": ("doc/multichip_decoder/README.md", "doc/multichip_decoder"),
    "optimized-multichip-decoder": (
        "doc/optimized_multichip_decoder/README.md",
        "doc/optimized_multichip_decoder",
    ),
    "full-model": ("doc/full_model/README.md", "doc/full_model"),
    "optimized-full-model": ("doc/optimized_full_model/README.md", "doc/optimized_full_model"),
    "datatype-sweep": ("doc/datatype_sweep/README.md", "doc/datatype_sweep"),
    "vllm": ("doc/vllm_integration/README.md", "doc/vllm_integration"),
    "optimized-vllm": ("doc/optimized_vllm/README.md", "doc/optimized_vllm"),
    "benchmark": ("doc/benchmark/run/REPORT.md", "doc/benchmark"),
    "agentic-evals": ("doc/agentic_evals/REPORT.md", "doc/agentic_evals"),
}
REPORT_RE = re.compile(r"\bdoc/([A-Za-z0-9_]+)(?:/[A-Za-z0-9_]+)*/(?:REPORT|README)\.md")


def stage_key(stem: str) -> str:
    return re.sub(r"^\d+-", "", stem)


def stage_paths(stem: str, prompt: str | None = None) -> tuple[str, str] | None:
    """(report, doc dir) relative to the model directory, or None when unknown."""
    known = STAGE_REPORTS.get(stage_key(stem))
    if known:
        return known
    match = REPORT_RE.search(prompt or "")
    if match:
        return match.group(0), f"doc/{match.group(1)}"
    return None


def parse_ts(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def fmt_ts(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_row() -> dict[str, Any]:
    row: dict[str, Any] = {"sessions": 0}
    row.update({name: 0 for name in FIELDS})
    row["models"] = []
    return row


def _usage(info: Any, key: str) -> dict[str, int] | None:
    if not isinstance(info, dict) or not isinstance(info.get(key), dict):
        return None
    return {name: int(info[key].get(name) or 0) for name in FIELDS}


def session_usage(path: pathlib.Path, start: dt.datetime, end: dt.datetime) -> dict[str, Any] | None:
    """Usage of one rollout file inside [start, end], or None when it has no event there."""
    meta: dict[str, Any] | None = None
    model: str | None = None
    previous_total: dict[str, int] | None = None
    sums = {name: 0 for name in FIELDS}
    models: set[str] = set()
    events = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = record.get("type")
            if kind == "session_meta" and meta is None:
                meta = payload
            elif kind == "turn_context" and isinstance(payload.get("model"), str):
                model = payload["model"]
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                last = _usage(info, "last_token_usage")
                total = _usage(info, "total_token_usage")
                if last is None:
                    continue
                repeat = total is not None and total == previous_total
                if total is not None:
                    previous_total = total
                stamp = parse_ts(record.get("timestamp"))
                if repeat or stamp is None or not start <= stamp <= end:
                    continue
                events += 1
                for name in FIELDS:
                    sums[name] += last[name]
                if model:
                    models.add(model)
    if not events:
        return None
    meta = meta or {}
    source = meta.get("source")
    subagent = isinstance(source, dict) and "subagent" in source
    return {
        "thread_id": meta.get("id"),
        "originator": meta.get("originator"),
        "subagent": subagent,
        "events": events,
        "models": sorted(models),
        **sums,
    }


def compute_usage(
    home: pathlib.Path,
    start: dt.datetime,
    end: dt.datetime,
    main_thread_ids: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Rows main_thread / sub_agents / other_threads / total for the window.

    Main thread: the threads the runner started for the stage (``main_thread_ids``); when
    none are given, any non-sub-agent thread the runner originated. Sub-agents: session_meta
    ``source.subagent`` at any depth. Other threads: everything else in that home.
    """
    ids = {str(item) for item in main_thread_ids if item}
    rows = {key: empty_row() for key in ROW_KEYS}
    models: dict[str, set[str]] = {key: set() for key in ROW_KEYS}
    for path in sorted((home / "sessions").glob("**/rollout-*.jsonl")):
        session = session_usage(path, start, end)
        if session is None:
            continue
        if session["subagent"]:
            key = "sub_agents"
        elif session["thread_id"] in ids or (not ids and session["originator"] == RUNNER_ORIGINATOR):
            key = "main_thread"
        else:
            key = "other_threads"
        for target in (key, "total"):
            rows[target]["sessions"] += 1
            for name in FIELDS:
                rows[target][name] += session[name]
            models[target].update(session["models"])
    for key, row in rows.items():
        row["uncached_input_tokens"] = row["input_tokens"] - row["cached_input_tokens"]
        row["models"] = sorted(models[key])
    return rows


def usage_record(
    *,
    stage: str,
    home: pathlib.Path,
    start: dt.datetime,
    end: dt.datetime,
    terminal_status: str,
    rounds: list[dict[str, Any]],
    report: str,
    main_thread_ids: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "schema": 1,
        "stage": stage,
        "codex_home": str(home),
        "start_utc": fmt_ts(start),
        "end_utc": fmt_ts(end),
        "wall_hours": round((end - start).total_seconds() / 3600, 2),
        "terminal_status": terminal_status,
        "remediation_rounds": rounds,
        "report": report,
        "rows": compute_usage(home, start, end, main_thread_ids),
    }


def _num(value: int) -> str:
    return f"{value:,}"


def render_section(record: dict[str, Any]) -> str:
    rounds = record["remediation_rounds"]
    listed = "; ".join(
        f"{item.get('kind', 'remediation')} {item['round']} {item['start_utc']} to "
        f"{item.get('end_utc') or 'running'}, {item.get('terminal_status') or 'running'}"
        for item in rounds
    )
    lines = [
        HEADING,
        "",
        "| row | sessions | input tokens | cached input | uncached input | output tokens | "
        "reasoning output | model(s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key in ROW_KEYS:
        row = record["rows"][key]
        label = ROW_LABELS[key]
        if key in ("sub_agents", "other_threads"):
            label = f"{label} ({row['sessions']})"
        lines.append(
            f"| {label} | {row['sessions']} | {_num(row['input_tokens'])} | "
            f"{_num(row['cached_input_tokens'])} | {_num(row['uncached_input_tokens'])} | "
            f"{_num(row['output_tokens'])} | {_num(row['reasoning_output_tokens'])} | "
            f"{', '.join(row['models']) or '-'} |"
        )
    lines += [
        "",
        f"Stage {record['start_utc']} to {record['end_utc']} (UTC), {record['wall_hours']:.2f} wall hours, "
        f"terminal status {record['terminal_status']}, remediation rounds {len(rounds)}"
        + (f" ({listed})" if listed else "")
        + ".",
        "",
        HOME_LINE.format(home=record["codex_home"]),
        "",
    ]
    return "\n".join(lines)


def replace_section(text: str, section: str) -> str:
    """Replace any previous ``## Stage usage`` sections with one, appended at the end."""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.strip() == HEADING:
            skipping = True
            continue
        if skipping and re.match(r"#{1,2} ", line):
            skipping = False
        if not skipping:
            kept.append(line)
    body = "\n".join(kept).rstrip()
    return (body + "\n\n" if body else "") + section


def write_usage(model_dir: pathlib.Path, report: str, doc_dir: str, record: dict[str, Any]) -> list[pathlib.Path]:
    """Write stage_usage.json and, when the report exists, its section. Returns written paths."""
    written: list[pathlib.Path] = []
    report_path = model_dir / report
    json_path = model_dir / doc_dir / "stage_usage.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(record, report_written=report_path.is_file())
    json_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    written.append(json_path)
    if report_path.is_file():
        report_path.write_text(
            replace_section(report_path.read_text(encoding="utf-8"), render_section(record)),
            encoding="utf-8",
        )
        written.append(report_path)
    return written


# ---------------------------------------------------------------- validation


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_record(record: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["stage_usage.json is not an object"]
    for key in ("codex_home", "terminal_status", "stage"):
        if not isinstance(record.get(key), str) or not record.get(key):
            errors.append(f"stage_usage.json {key} must be a non-empty string")
    start, end = parse_ts(record.get("start_utc")), parse_ts(record.get("end_utc"))
    if start is None or end is None or end < start:
        errors.append("stage_usage.json start_utc/end_utc must be UTC timestamps with end >= start")
    if not isinstance(record.get("wall_hours"), (int, float)) or record.get("wall_hours", -1) < 0:
        errors.append("stage_usage.json wall_hours must be a number >= 0")
    if not isinstance(record.get("remediation_rounds"), list):
        errors.append("stage_usage.json remediation_rounds must be a list")
    rows = record.get("rows")
    if not isinstance(rows, dict) or any(not isinstance(rows.get(key), dict) for key in ROW_KEYS):
        return errors + [f"stage_usage.json rows must have {', '.join(ROW_KEYS)}"]
    for key in ROW_KEYS:
        row = rows[key]
        for name in ("sessions", *FIELDS, "uncached_input_tokens"):
            if not _int(row.get(name)):
                errors.append(f"stage_usage.json rows.{key}.{name} must be an integer >= 0")
        if not isinstance(row.get("models"), list):
            errors.append(f"stage_usage.json rows.{key}.models must be a list")
        if _int(row.get("input_tokens")) and _int(row.get("cached_input_tokens")) and _int(
            row.get("uncached_input_tokens")
        ) and row["uncached_input_tokens"] != row["input_tokens"] - row["cached_input_tokens"]:
            errors.append(f"stage_usage.json rows.{key} uncached != input - cached")
    if not errors:
        for name in ("sessions", *FIELDS):
            parts = sum(rows[key][name] for key in ROW_KEYS if key != "total")
            if parts != rows["total"][name]:
                errors.append(f"stage_usage.json total {name} {rows['total'][name]} != sum of rows {parts}")
    return errors


def validate_section(text: str, record: dict[str, Any]) -> list[str]:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == HEADING]
    if len(starts) != 1:
        return [f"report must have exactly one '{HEADING}' section (found {len(starts)})"]
    section: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if re.match(r"#{1,2} ", line):
            break
        section.append(line)
    errors: list[str] = []
    table = {}
    for line in section:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.lstrip().startswith("|") and len(cells) == 8:
            table[re.sub(r"\s*\(\d+\)$", "", cells[0])] = cells
    for key in ROW_KEYS:
        cells = table.get(ROW_LABELS[key])
        if cells is None:
            errors.append(f"'{HEADING}' table has no '{ROW_LABELS[key]}' row")
            continue
        row = record["rows"][key]
        expected = [row["sessions"], row["input_tokens"], row["cached_input_tokens"],
                    row["uncached_input_tokens"], row["output_tokens"], row["reasoning_output_tokens"]]
        try:
            found = [int(cell.replace(",", "")) for cell in cells[1:7]]
        except ValueError:
            errors.append(f"'{HEADING}' {ROW_LABELS[key]} row has non-numeric cells")
            continue
        if found != expected:
            errors.append(f"'{HEADING}' {ROW_LABELS[key]} row does not match stage_usage.json")
    joined = "\n".join(section)
    if not re.search(r"^Stage \S+ to \S+ \(UTC\), [\d.]+ wall hours, terminal status \S+, remediation rounds \d+",
                     joined, re.M):
        errors.append(f"'{HEADING}' lacks the stage start/end, wall hours, status and rounds line")
    if HOME_LINE.format(home=record["codex_home"]) not in joined:
        errors.append(f"'{HEADING}' lacks the 'Counted from the Codex sessions in <CODEX_HOME>' line")
    return errors


def check(model_dir: pathlib.Path, stem: str) -> tuple[int, list[str]]:
    paths = stage_paths(stem)
    if paths is None:
        return 3, [f"unknown stage {stem!r}"]
    report, doc_dir = paths
    json_path = model_dir / doc_dir / "stage_usage.json"
    if not json_path.is_file():
        return 2, [f"{json_path} is missing (the runner writes it when the stage ends)"]
    try:
        record = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return 2, [f"{json_path} is not valid JSON: {exc}"]
    errors = validate_record(record)
    if errors:
        return 2, errors
    report_path = model_dir / report
    if not report_path.is_file():
        return 2, [f"{report_path} is missing, so it has no '{HEADING}' section"]
    errors = validate_section(report_path.read_text(encoding="utf-8", errors="replace"), record)
    return (2 if errors else 0), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("check", help="validate a stage's usage section and json")
    gate.add_argument("--model-dir", default="")
    gate.add_argument("--stage", required=True, help="prompt stem, e.g. 12-agentic-evals")
    show = sub.add_parser("compute", help="print the section for a CODEX_HOME and window")
    show.add_argument("--codex-home", type=pathlib.Path, required=True)
    show.add_argument("--start", required=True)
    show.add_argument("--end", required=True)
    show.add_argument("--stage", default="stage")
    show.add_argument("--terminal-status", default="complete")
    show.add_argument("--main-thread-id", action="append", default=[])
    show.add_argument("--round", action="append", default=[], metavar="START,END,STATUS",
                      help="a remediation round inside the window (repeatable)")
    show.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "compute":
        start, end = parse_ts(args.start), parse_ts(args.end)
        if start is None or end is None:
            parser.error("--start/--end must be ISO timestamps")
        rounds = []
        for number, item in enumerate(args.round, start=1):
            round_start, round_end, status = item.split(",")
            rounds.append({"kind": "remediation", "round": number, "start_utc": round_start,
                           "end_utc": round_end, "terminal_status": status})
        record = usage_record(stage=args.stage, home=args.codex_home, start=start, end=end,
                              terminal_status=args.terminal_status, rounds=rounds, report="",
                              main_thread_ids=args.main_thread_id)
        print(json.dumps(record, indent=2) if args.json else render_section(record))
        return 0

    # The runner sets MULTIGOAL_STAGE_USAGE when it runs a check after its write. An agent
    # running the check itself mid-stage is not judged on a section only the runner writes.
    if not os.environ.get("MULTIGOAL_STAGE_USAGE"):
        print("stage usage: not checked (written and gated by the multigoal runner)")
        return 0
    if not args.model_dir:
        print("stage usage: MODEL_DIR is required", file=sys.stderr)
        return 3
    code, errors = check(pathlib.Path(args.model_dir), args.stage)
    for error in errors:
        print(f"stage usage: {error}", file=sys.stderr)
    if code == 0:
        print("stage usage: ok")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
