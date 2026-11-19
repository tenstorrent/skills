# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Whether another project can act now, model-free: before an ETA that waits on it is promised.

state() reads a project on this machine from its own records, read-only: its pause (projpause) and
the daemon's last spend gate of its core provider (kv `gates`, budget.hold_clears), and gives
{"held", "why", "until"}. A project on another machine has none of these here (account spend sync
carries dollars only), so it reads as unknown, never as free. line() puts it in words for `ttp note
--to`, the note's facts suffix and the digest's promise lines (obligations.promise_lines), which flag a
due earlier than the resume time of a project the promise's owner tasks sent notes to (the inbox's
recorded sender task and target, never the note's wording).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class _RO:
    """The DB calls budget.hold_clears and projpause.current make, on a read-only connection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.row_factory = sqlite3.Row

    def one(self, sql: str, params=()):
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def kv(self, key: str, default=None):
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        try:
            return json.loads(r[0]) if r and r[0] is not None else default
        except ValueError:
            return default


def state(p, now: float | None = None) -> dict | None:
    """{"held": bool, "why": str, "until": epoch or None} of project `p` (a Project), or None when its
    records cannot be read here. `until` None while held: no known end (the user or a cap lifts it)."""
    from . import budget as bud, projpause
    from .globalcap import connect_ro
    now = time.time() if now is None else now
    path = Path(p.state) / "project.db"
    if not path.is_file():
        return None
    try:
        conn = connect_ro(path, timeout=2)
    except sqlite3.Error:
        return None
    try:
        db = _RO(conn)
        paused = projpause.current(db, now)
        if paused:
            until = _when(paused.get("end"))
            return {"held": True, "until": until,
                    "why": "paused" + (f" by {paused['by']}" if paused.get("by") else "")
                    + (f": {paused['reason']}" if paused.get("reason") else "")}
        try:
            core = (p.config.get("core_provider") or "claude")
        except Exception:
            core = "claude"
        gates = db.kv("gates", {})
        g = gates.get(core) if isinstance(gates, dict) else None
        if not isinstance(g, dict) or g.get("allow_new_work", True) is not False:
            return {"held": False, "why": "", "until": None}
        gate = bud.Gate(provider=core, reasons=[r for r in g.get("reasons") or [] if isinstance(r, str)],
                        numbers=g.get("numbers") if isinstance(g.get("numbers"), dict) else {})
        lim = db.kv(f"limited:{core}")
        until, clears = bud.hold_clears(db, gate, now, lim.get("until") if isinstance(lim, dict) else None)
        why = f"{core} spend gate: " + "; ".join(gate.reasons[:2]) if gate.reasons else f"{core} spend gate"
        return {"held": True, "until": until, "why": f"{why} (clears when {clears})"}
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        return None
    finally:
        conn.close()


def _when(end) -> float | None:
    """A pause's end as epoch seconds when it is a time (epoch or ISO with an offset); None for words."""
    from datetime import datetime
    if isinstance(end, (int, float)) and not isinstance(end, bool):
        return float(end)
    try:
        got = datetime.fromisoformat(str(end).strip())
    except ValueError:
        return None
    return got.timestamp() if got.tzinfo else None


def of(name: str, now: float | None = None) -> dict | None:
    """state() of the project `name` on this machine; None when it is not here or unreadable."""
    from .cli import local_project
    try:
        p = local_project(name)
    except Exception:
        return None
    return state(p, now) if p is not None and p.exists() else None


def line(name: str, st: dict | None, where, now: float | None = None) -> str:
    """'<name> held until <time> (<why>)', '<name> held, no known end (<why>)', or '' when it runs or
    is unknown here."""
    from . import timefmt
    if not st or not st.get("held"):
        return ""
    when = (f"until {timefmt.short(st['until'], where, now)}" if st.get("until")
            else "with no known end")
    return f"{name} held {when} ({st.get('why') or 'held'})"


def targets(sender: str, tasks, path: Path | None = None) -> list[str]:
    """The projects the notes of `sender`'s tasks `tasks` went to, from the inbox's recorded sender,
    task and target fields (upstream.send)."""
    from . import upstream
    want = {int(t) for t in tasks or []}
    if not want:
        return []
    out: list[str] = []
    try:
        with open(path or upstream.path()) as f:
            for raw in f:
                try:
                    n = json.loads(raw)
                except ValueError:
                    continue
                if (isinstance(n, dict) and n.get("project") == sender and isinstance(n.get("task"), int)
                        and n["task"] in want and isinstance(n.get("to"), str) and n["to"] != sender
                        and n["to"] not in out):
                    out.append(n["to"].split("@", 1)[0])
    except OSError:
        return []
    return list(dict.fromkeys(out))


def promise_note(sender: str, o: dict, where, now: float, lookup=of) -> str:
    """For a promise whose owner tasks sent notes to other projects: '; <x> held until <t>' for each held
    one, with 'DUE BEFORE IT RESUMES' when the promise's due is earlier and its text does not quote that
    resume time; '' when none is held."""
    from . import timefmt
    from .obligations import _owner_ids
    pr = o.get("promise") or {}
    out = []
    for name in targets(sender, _owner_ids(o)):
        st = lookup(name, now)
        words = line(name, st, where, now)
        if not words:
            continue
        until = st.get("until")
        quoted = bool(until) and timefmt.short(until, where, now).split(" ")[-2] in (o.get("text") or "")
        if until and pr.get("due") and pr["due"] < until and not quoted:
            words += ": DUE BEFORE IT RESUMES (quote its resume time or move the due)"
        elif not until and pr.get("due"):
            words += ": the due waits on a hold with no known end"
        out.append(words)
    return "".join(f"; {w}" for w in out)
