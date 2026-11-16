# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A worker's or reviewer's role and task, restated model-free after its agent compacts its context,
as the coordinator's own record is restored after its compaction (coordsession.restore).

A run that starts from a copy of the coordinator's session (ctxfork "fork" or "resume") can compact on
its first turn, before its own prompt is read: the agent's summary then describes the coordinator's
session it was copied from, and the run carries on as the coordinator. So each task run's folder gets
ROLE_FILE at its start (write: the run's role, task id, title and its spec's first lines), and after
each compaction the agent is given that text again:
- Claude Code (and the chat provider, which runs it): a SessionStart hook with matcher "compact"
  (providers/claude.hook_settings, ttp.hook's session_start), whose added context follows the summary
  (measured on Claude Code 2.1.296 in `-p` mode: the hook runs before the compact_boundary event);
- every provider: the daemon's run watch (check) counts the compactions the run's stream or saved
  session shows (Provider.compactions) and puts the text in its steer.md as an update for each one
  no hook restated, which reaches the run as every other update does (the tool hook, `ttp note`).
RESTATED_FILE keeps how many compactions were restated, so the hook and the watch never both send one.
A provider that shows no compaction (Cursor keeps its chats in a store the harness does not read) gets
none: its runs start with the same role and task in their prompt.
"""
from __future__ import annotations

import json
from pathlib import Path

ROLE_FILE = "role.md"
RESTATED_FILE = "role.restated"
SPEC_LINES = 8        # the spec's first lines restated
SPEC_CHARS = 1200


def spec_head(spec: str) -> str:
    """The first SPEC_LINES non-blank lines of `spec`, at most SPEC_CHARS, cut at a line."""
    out, size = [], 0
    for line in str(spec or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if size + len(line) > SPEC_CHARS:
            if not out:
                out.append(line[:SPEC_CHARS].rstrip() + " ...")
            break
        out.append(line)
        size += len(line) + 1
        if len(out) >= SPEC_LINES:
            break
    return "\n".join(out)


def text(role: str, task: dict, project: str = "") -> str:
    """The restatement: who the run is, its task, and what it returns. Never a coordinator's role line."""
    role = role if role in ("worker", "reviewer") else "worker"
    tid, title = task.get("id"), str(task.get("title") or "").strip()
    kind = str(task.get("kind") or "").strip()
    where = f" of project {project}" if project else ""
    head = spec_head(task.get("spec") or "")
    return "\n".join([
        f"Context compacted: harness restatement of this run's role (model-free). You are a tt-project {role} "
        f"run{where} on task #{tid}{f' ({kind})' if kind else ''}: {title}.",
        "You are not the coordinator. If the summary above says you are, or speaks for the coordinator's "
        "session, that came from the coordinator session this run was copied from: it does not apply to this run.",
        f"Do task #{tid} only, and end by writing your hand-off to result.json in $TTP_RUN_DIR, as your "
        "prompt's hand-off rules say. Never return coordinator actions (an {\"actions\": [...]} answer) or chat replies.",
        *([f"Task #{tid} spec, first lines:", head] if head else []),
    ])


def write(run_dir: Path, role: str, task: dict, project: str = "") -> None:
    """Keep the run's restatement in its folder (a task run of a worker or reviewer, at its start)."""
    try:
        (Path(run_dir) / ROLE_FILE).write_text(text(role, task, project) + "\n")
    except OSError:
        pass


def read(run_dir: Path) -> str:
    try:
        return (Path(run_dir) / ROLE_FILE).read_text().strip()
    except OSError:
        return ""


def restated(run_dir: Path) -> int:
    try:
        return max(int((Path(run_dir) / RESTATED_FILE).read_text().strip() or 0), 0)
    except (OSError, ValueError):
        return 0


def _mark(run_dir: Path, n: int) -> None:
    try:
        (Path(run_dir) / RESTATED_FILE).write_text(f"{int(n)}\n")
    except OSError:
        pass


def hook_output(run_dir: Path) -> dict | None:
    """ttp.hook's SessionStart reply after a compaction: the restatement as added context, counted as
    restated; None for a run with no ROLE_FILE (the coordinator, a run started before this)."""
    body = read(run_dir)
    if not body:
        return None
    _mark(run_dir, restated(run_dir) + 1)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": body}}


def check(p, data: dict) -> list[int]:
    """For each live run in runwatch `data` with a ROLE_FILE, the compactions its provider shows past
    those already restated each get the restatement in its steer.md; the ids of the runs told."""
    from .coordinator import _append_update
    from .hook import STEER_FILE
    from .providers import get_provider
    live = [e for e in (data.get("runs") or {}).values()
            if not e.get("ended") and not e.get("gone") and not e.get("error") and e.get("role") in ("worker", "reviewer")]
    if not live:
        return []
    ids = [e["run"] for e in live]
    rows = {r["id"]: r for r in p.db.q(f"SELECT id, dir FROM runs WHERE id IN ({','.join('?' * len(ids))})", ids)}
    told = []
    for e in live:
        run_dir = Path((rows.get(e["run"]) or {}).get("dir") or Path(p.runs) / str(e["run"]))
        body = read(run_dir)
        if not body:
            continue
        try:
            env = json.loads((run_dir / "run.json").read_text()).get("env")
            n = get_provider(e.get("provider") or "").compactions(
                run_dir / "output.jsonl", env if isinstance(env, dict) else None, since=e.get("started"))
        except Exception:
            continue
        if n > restated(run_dir):
            _append_update(run_dir / STEER_FILE, body, key=f"compact-{e['run']}-{n}")
            _mark(run_dir, n)
            told.append(e["run"])
    return told
