# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The session coordinator's hands-on fixes, on the record (user msg #1776: it fixes the work, the
workers and the project's own prompts, scripts, charter notes and config itself).

A full-tools coordinator's mutating calls are logged to its run's AUDIT file (coordtools.post_tool_use).
Before a fix it runs `ttp audit why "<why>" --serves <ref> --undo "<how>"` (cmd_audit): that writes the
run's INTENT, and every mutating call after it in the same run (one wake) carries the why, the order or
task it serves (`o171`, `msg#1776` or `#6302`) and the undo, until the next `ttp audit why` or
`ttp audit why --done`. Each entry's what is the Bash call's description, else its tool and target.

A mutating call with no why is flagged once, in the next coordinator digest (digest_lines: the calls
since the last turn started); the daily review lists the day's fixes grouped by the order they serve
(audit_line), and `ttp audit fixes [--since <time>]` prints them (fixes_text)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .coordtools import AUDIT

INTENT = "coord-intent.json"   # in the run dir: the why the next mutating calls carry
NO_WHY = "no why recorded"
SHOWN = 8   # entries per group in one line


def serves(raw: str) -> str:
    """The order or task a fix serves, normalised: `o171` (an obligation), `user msg #1776` or
    `task #6302`. ValueError for anything else."""
    s = re.sub(r"\s+", "", str(raw or "")).lower()
    if m := re.fullmatch(r"o(\d+)", s):
        return f"o{int(m[1])}"
    if m := re.fullmatch(r"(?:user)?(?:msg|message|m)#?(\d+)", s):
        return f"user msg #{int(m[1])}"
    if m := re.fullmatch(r"(?:task)?#?(\d+)", s):
        return f"task #{int(m[1])}"
    raise ValueError(f"--serves {raw!r}: name an obligation (o171), a user message (msg#1776) or a task (#6302)")


def declare(run_dir: str | Path, why: str, serves_ref: str, undo: str) -> dict:
    """Write the run's intent (module doc); ValueError on a missing field or a bad --serves."""
    why, undo = " ".join(str(why or "").split()), " ".join(str(undo or "").split())
    if not why:
        raise ValueError("say why: ttp audit why \"<why>\" --serves <ref> --undo \"<how>\"")
    if not undo:
        raise ValueError("--undo: the command or steps that undo it (`none: <why>` when nothing can)")
    rec = {"ts": time.time(), "why": why[:500], "serves": serves(serves_ref), "undo": undo[:500]}
    from .project import durable_write
    durable_write(Path(run_dir) / INTENT, json.dumps(rec))
    return rec


def done(run_dir: str | Path) -> None:
    """End the run's intent: later mutating calls carry no why until the next declare."""
    (Path(run_dir) / INTENT).unlink(missing_ok=True)


def intent(run_dir: str | Path) -> dict:
    """The run's current {why, serves, undo}, or {} (none, or unreadable)."""
    try:
        rec = json.loads((Path(run_dir) / INTENT).read_text())
        return {k: str(rec[k]) for k in ("why", "serves", "undo") if rec.get(k)} if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def entries(db, since: float, until: float | None = None) -> list[dict]:
    """The coordinator's logged mutating calls with since <= ts < until, oldest first, each with
    its run id; a run dir that is gone or a bad line is skipped."""
    until = time.time() + 1 if until is None else until
    rows = db.q("SELECT id, dir FROM runs WHERE role='coordinator' AND dir IS NOT NULL AND started<? "
                "AND (ended IS NULL OR ended>=?) ORDER BY id", (until, since))
    out = []
    for r in rows:
        try:
            text = (Path(r["dir"]) / AUDIT).read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                e = json.loads(line)
                ts = float(e.get("ts") or 0)
            except (ValueError, TypeError, AttributeError):
                continue
            if since <= ts < until:
                out.append({**e, "ts": ts, "run": r["id"]})
    return sorted(out, key=lambda e: e["ts"])


def what(e: dict) -> str:
    w = " ".join(str(e.get("what") or f"{e.get('tool') or '?'} {e.get('target') or ''}").split())
    return w[:160] + (f" (exit {e['exit']})" if e.get("exit") not in (0, None) else "")


def _groups(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """(order or task, its entries) for rows with a why, by first use; then NO_WHY's."""
    groups: dict[str, list[dict]] = {}
    for e in rows:
        groups.setdefault(str(e.get("serves") or "?") if e.get("why") else NO_WHY, []).append(e)
    return sorted(groups.items(), key=lambda kv: kv[0] == NO_WHY)


def digest_lines(db, since: float, now: float | None = None) -> list[str]:
    """The calls since `since` (the last coordinator turn's start) that carried no why: shown once."""
    missing = [e for e in entries(db, since, now) if not e.get("why")]
    if not missing:
        return []
    return ([f"## Hands-on changes with no recorded why: {len(missing)} (say why in this turn; before each fix run "
             "`ttp audit why \"<why>\" --serves <o12|msg#123|#456> --undo \"<how>\"`)"]
            + [f"- run {e['run']}: {what(e)}" for e in missing[:SHOWN]]
            + ([f"- and {len(missing) - SHOWN} more (`ttp audit fixes`)"] if len(missing) > SHOWN else []))


def audit_line(db, since: float, now: float | None = None) -> str:
    """The daily review's line: the window's fixes grouped by the order they serve, each with its
    why and undo; "" when there were none."""
    rows = entries(db, since, now)
    if not rows:
        return ""
    parts = []
    for key, es in _groups(rows):
        shown = "; ".join(what(e) + (f" (why: {e['why'][:120]}; undo: {e.get('undo', '')[:80]})" if e.get("why") else "")
                          for e in es[:SHOWN])
        parts.append(f"{key} ({len(es)}): {shown}" + (f"; and {len(es) - SHOWN} more" if len(es) > SHOWN else ""))
    return f"coordinator hands-on fixes: {len(rows)}, by order: " + " | ".join(parts)


def fixes_text(db, since: float, zone, now: float | None = None) -> str:
    """`ttp audit fixes`: every entry in the window, grouped by the order it serves, with time,
    run, what, why and undo."""
    from . import timefmt
    rows = entries(db, since, now)
    if not rows:
        return f"no coordinator hands-on fixes since {timefmt.long(since, zone)}"
    out = []
    for key, es in _groups(rows):
        out.append(f"{key}: {len(es)}")
        for e in es:
            out.append(f"  {timefmt.long(e['ts'], zone)} run {e['run']} {what(e)}")
            if e.get("why"):
                out.append(f"    why: {e['why']}\n    undo: {e.get('undo') or '-'}")
    return "\n".join(out)
