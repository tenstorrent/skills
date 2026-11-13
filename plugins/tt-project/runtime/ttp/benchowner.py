# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model bench owner: one project per machine owns the shared daily chat model bench (modelbench.py).

The lease is modelbench.OWNER, {project, since, last_result, tried, poke, prev}, changed only under
modelbench.owner_update. Every daemon checks it hourly without a model call (check). Eligible projects
(eligible) are the machine's registered projects with a live daemon heartbeat, a chat endpoint, not
paused and without model_bench.opt_out, in name order; with no owner the first takes it. The lease's age
runs from the newest of since, last_result and tried (a recorded keyless skip counts: the owner ran).
Older than model_bench.poke_after_h: one poke per episode, which the owner's daemon turns into running
its bench schedule now, with one feed line in both projects. Older than take_over_after_h, or the owner
not eligible: the next eligible project after it by name takes the lease, with one low alert naming
both. Other projects' databases and settings are only read. The owner keeps the command schedule
SCHEDULE (`ttp model-bench run --detach`, daily at bench_at home time) and SUITE_SCHEDULE (`ttp
model-bench suite --detach`, an hour later, which first rebuilds its bench folders when stale); every
other project's are off.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEDULE = "model-bench"
COMMAND = "ttp model-bench run --detach"
SUITE_SCHEDULE = "model-suite"
SUITE_COMMAND = "ttp model-bench suite --detach"
LIVE_S = 900                       # a daemon heartbeat younger than this is a live daemon
SEEN = "model-bench-owner-seen"    # kv: the poke this project's daemon handled last


def settings(cfg: dict) -> dict:
    s = cfg.get("model_bench") if isinstance(cfg.get("model_bench"), dict) else {}

    def hours(key: str, default: float) -> float:
        try:
            return max(float(s.get(key, default)), 1.0) * 3600
        except (TypeError, ValueError):
            return default * 3600
    return {"opt_out": bool(s.get("opt_out")), "poke_s": hours("poke_after_h", 48),
            "take_s": hours("take_over_after_h", 72), "at": str(s.get("bench_at") or "02:00")}


def _paused(db: Path) -> bool:
    from .globalcap import connect_ro
    conn = connect_ro(db)
    try:
        row = conn.execute("SELECT value FROM kv WHERE key='paused'").fetchone()
        return bool(row and json.loads(row[0]))
    finally:
        conn.close()


def eligible(now: float) -> list[str]:
    """This machine's projects that may own the bench, by name (module doc); read only."""
    import sqlite3
    from . import globalcap, project, rankfresh
    out = []
    for name, db in globalcap.local_projects():
        folder = db.parent.parent
        try:
            if now - (folder / "state" / "heartbeat").stat().st_mtime > LIVE_S:
                continue
            raw = json.loads((folder / "harness" / "project.json").read_text())
            cfg = project.layered(raw if isinstance(raw, dict) else {})
            if rankfresh.configured(cfg) and not settings(cfg)["opt_out"] and not _paused(db):
                out.append(name)
        except (OSError, ValueError, sqlite3.Error):
            continue   # unreadable: not eligible this hour
    return out


def ref(owner: dict) -> float:
    """When the lease last showed life: its newest of since, last_result and tried."""
    return max(float(owner.get(k) or 0) for k in ("since", "last_result", "tried"))


def check(me: str, cfg: dict, now: float, names: list[str] | None = None) -> dict:
    """One hourly check by project `me`. Returns {"owner"} plus {"took": old owner} when `me` took the
    lease, or {"poked": owner} when `me` poked it. `names` replaces eligible() (tests)."""
    from . import modelbench
    s = settings(cfg)
    got: dict = {}

    def step(owner: dict) -> dict:
        ok = eligible(now) if names is None else sorted(names)
        if me not in ok:
            got["owner"] = owner.get("project")
            return owner
        cur = owner.get("project")
        nxt = ([n for n in ok if n > (cur or "")] + ok)[0] if cur else ok[0]
        if not cur or cur not in ok or now - ref(owner) > s["take_s"]:
            if nxt == me and cur != me:   # the next by name takes it; any other project waits for it
                got.update(owner=me, took=cur or "")
                keep = {k: owner[k] for k in ("nokey_since",) if k in owner}   # a keyless outage is reported once
                return {**keep, "project": me, "since": now, "prev": cur or ""}
        elif now - ref(owner) > s["poke_s"] and cur != me and (owner.get("poke") or {}).get("ref") != ref(owner):
            got["poked"] = cur   # one poke per episode: until the lease shows life again
            owner = {**owner, "poke": {"ref": ref(owner), "at": now, "by": me}}
        got.setdefault("owner", owner.get("project"))
        return owner
    modelbench.owner_update(step)
    return got


def _later(at: str, hours: int = 1) -> str:
    try:
        h, m = (int(x) for x in at.split(":"))
    except ValueError:
        h, m = 2, 0
    return f"{(h + hours) % 24:02d}:{m:02d}"


def ensure(p, db, owner: bool, at: str) -> str:
    """The owner keeps SCHEDULE on, daily at `at`, and SUITE_SCHEDULE an hour later; any other project
    keeps both off. Returns what changed."""
    from . import schedule as sched, timefmt
    want = [(SCHEDULE, COMMAND, at, "the machine's shared daily chat model bench (this project owns it)"),
            (SUITE_SCHEDULE, SUITE_COMMAND, _later(at),
             "the machine's shared daily model suite, bench folders kept fresh (this project owns it)")]
    todo = []
    for name, cmd, when, desc in want:
        row = db.one("SELECT enabled, at, payload FROM schedules WHERE name=?", (name,))
        if not owner and not (row and row["enabled"]):
            continue
        if owner and row and row["enabled"] and row["at"] == when \
                and json.loads(row["payload"] or "{}").get("command") == cmd:
            continue
        todo.append((name, cmd, when, desc))
    if not todo:
        return ""
    sched.before_change(p)
    for name, cmd, when, desc in todo:
        if owner:
            sched.upsert(db, name, "command", "1d", when, True, None, desc, {"command": cmd}, timefmt.home(p))
        else:
            sched.set_enabled(db, name, False, home=timefmt.home(p))
    did = "on" if owner else "off"
    try:
        sched.write_file(p, f"schedule {', '.join(t[0] for t in todo)}: {did} (model bench owner)")
    except Exception:
        pass
    return did