# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The periodic de-slop pass: the `deslop` llm schedule, kept on only while `deslop.enabled` is true.

`ensure` (hourly, from the daemon) adds or removes the schedule. Each pass (`plan`, called by the daemon's
schedule run, model-free) scans the project's base branch (slop.py), records the numbers against the
targets, skips every subsystem refactored within `deslop.gap_days`, picks the worst of the rest (narrowed to
its worst part while it is over `deslop.max_pass_lines`, slop.pass_target) and returns the spec of one refactor
code task. No pass runs while the project is paused, and none queues a second task
while the last one is open (the schedule's own one-open-run rule). The task's spec tells its worker to
change no behaviour, keep the tests green, respect holds, change freezes and settled areas, and never run
device or machine work; its review compares the before and after numbers the spec carries.

State, kv `deslop`: {"baseline": the first scan's rows, "last_scan": the newest, "passes": [{at,
subsystem, task, ref, before, excess}] newest last}.
"""
from __future__ import annotations

import json
import time

from . import slop

SCHEDULE = "deslop"
KEY = "deslop"
KEEP_PASSES = 20
PAYLOAD = {"kind": "code", "tier": "standard", "priority": 4, "deslop": True, "skip_if_idle": False}
DESCRIPTION = "Periodic de-slop pass: one no-behaviour-change refactor of the subsystem furthest over its code-quality targets"
OFF_SKIP = "skipped: deslop.enabled is off"
ROW_KEYS = ("lines", "duplicates", "globals", "magic", "magic_per_kloc", "long_functions", "fn_max", "noise_share")


def settings(cfg: dict) -> dict:
    """deslop.* over slop.DEFAULTS, thresholds merged key by key."""
    mine = cfg.get("deslop") if isinstance(cfg.get("deslop"), dict) else {}
    out = {**slop.DEFAULTS, **mine}
    out["thresholds"] = {**slop.DEFAULTS["thresholds"], **(mine.get("thresholds") or {})}
    return out


def ensure(p, db, on: bool, every: str) -> str:
    """Keep the schedule on, every `every`, while `on`; off otherwise. Returns what changed ('' none).
    A schedule that never made a pass is due at once, once: the first pass is not a whole `every` away, nor is
    it after a run that only skipped because the flag was off."""
    from . import schedule as sched, timefmt
    row = db.one("SELECT enabled, every_s, payload, last_run, last_status FROM schedules WHERE name=?", (SCHEDULE,))
    if not on and not (row and row["enabled"]):
        return ""
    did = ""
    if on and not (row and row["enabled"] and row["every_s"] == sched.parse_every(every)
                   and json.loads(row["payload"] or "{}") == PAYLOAD):
        sched.before_change(p)
        sched.upsert(db, SCHEDULE, "llm", every, None, True, None, DESCRIPTION, dict(PAYLOAD), timefmt.home(p))
        did = "on"
    elif not on:
        sched.before_change(p)
        sched.set_enabled(db, SCHEDULE, False, home=timefmt.home(p))
        did = "off"
    state = db.kv(KEY, {}) or {}
    ran = float(row["last_run"] or 0) if row else 0.0
    off_skip = bool(row) and str(row["last_status"] or "").startswith(OFF_SKIP)
    if on and not state.get("passes") and (not ran or off_skip) and float(state.get("kicked") or 0) <= ran:
        db.x("UPDATE schedules SET next_run=? WHERE name=?", (time.time(), SCHEDULE))
        db.set_kv(KEY, {**state, "kicked": time.time()})
        did = did or "due"
    if did:
        try:
            sched.write_file(p, f"schedule {SCHEDULE}: {did} (deslop.enabled)")
        except Exception:   # the database holds it; the file catches up on the next change
            pass
    return did


def _pass_row(sub: dict, thresholds: dict) -> dict:
    return {**{k: sub.get(k, 0) for k in ROW_KEYS}, "excess": slop.excess(sub, thresholds)}


def _rows(rep: dict, thresholds: dict) -> dict:
    return {n: _pass_row(s, thresholds) for n, s in rep["subsystems"].items()}


def _base(p) -> str | None:
    from . import worktree
    try:
        return worktree.resolve_base(p)
    except RuntimeError:
        return None   # no named base: the working tree stands for it


def _numbers(row: dict, c: dict) -> str:
    t, m = c["thresholds"], c["max_function_lines"]
    return (f"{row['lines']} lines; duplicate rules {row['duplicates']} (target {t['duplicates']}); mutable globals "
            f"{row['globals']} (target {t['globals']}); magic numbers {row['magic_per_kloc']}/kloc (target "
            f"{t['magic_per_kloc']}); functions over {m} lines {row['long_functions']} (target "
            f"{t['long_functions']}), longest {row['fn_max']}; incident-style comment share {row['noise_share']} "
            f"(target {t['noise_share']})")


def _settled_files(p) -> set[str]:
    """The files of the project's live settled areas: never a pass's target."""
    from . import settled
    return {f for r in settled.live(p) for f in r.get("files") or []}


def spec(p, c: dict, sub: str, row: dict, ref: str | None) -> str:
    frozen = _settled_files(p)
    lines = [
        f"Goal: one de-slop refactor of the subsystem `{sub}`, chosen by the model-free scan (`ttp slop {p.name}`) "
        "as the one furthest over its code-quality targets and not refactored lately. No behaviour change.",
        f"Numbers now (scan of {ref or 'the working tree'}): {_numbers(row, c)}.",
        f"Acceptance: cut each over-target number above by at least {c['step_share']:.0%}, or to its target. Merge "
        "duplicate rules into the one function that decides them and delete the copies; name magic numbers as "
        "constants where they are set; split functions over the length target at their natural seams; rewrite "
        "incident-diary comments (a date, a task or ticket id, a host name) as the rule they protect, or delete "
        "them; delete tests that only replay one recorded incident and keep or add the ones that check the rule. "
        "Behaviour stays identical: the existing tests pass unchanged, no new special case, no new feature. A "
        "number that cannot move without a behaviour change stays, with one line in the hand-off saying why.",
        "Rules: respect every hold, pause, change freeze and settled area the charter or the digest names"
        + (f" (settled areas now: {', '.join(sorted(set(frozen))[:12])}; leave those files alone)" if frozen else "")
        + "; if the subsystem is frozen, do nothing and say so in the hand-off. Never run device or machine work. "
        "Leave generated copies and vendored code alone. Run only the affected tests while iterating, then the "
        "project's checks once.",
        f"Hand-off: the before and after numbers of `{sub}` (run `ttp slop {p.name} --ref <head> --subsystem {sub}` "
        "and read its row) and the commits. Its review checks those numbers and fails a change whose numbers did not improve "
        "or that changed behaviour.",
    ]
    return "\n\n".join(lines)


def plan(p, cfg: dict, now: float | None = None) -> dict | str:
    """The next pass: {title, spec, subsystem, ref, row, excess}, or why none runs now ('skipped: ...')."""
    from . import projpause
    now = time.time() if now is None else now
    c, db = settings(cfg), p.db
    if not c["enabled"]:
        return OFF_SKIP
    if projpause.current(db):
        return "skipped: the project is paused"
    ref = _base(p)
    infos = slop.scan_files(p.root, ref, c, None, str(p.state / "slop-cache.json"))
    rows = _rows(slop.aggregate(infos, c), c["thresholds"])
    state = db.kv(KEY, {}) or {}
    state["last_scan"] = {"at": now, "ref": ref, "subsystems": rows}
    state.setdefault("baseline", state["last_scan"])
    recent = {x.get("subsystem") for x in state.get("passes") or [] if now - float(x.get("at") or 0) < c["gap_days"] * 86400}
    pick = slop.pass_target(infos, c, recent | _settled_files(p))
    db.set_kv(KEY, state)
    if not pick:
        return "skipped: every subsystem is under its targets or was refactored lately"
    sub, excess, numbers = pick
    row = _pass_row(numbers, c["thresholds"])
    return {"title": f"[{SCHEDULE}] refactor {sub}: {excess} over targets, no behaviour change", "spec": spec(p, c, sub, row, ref),
            "subsystem": sub, "ref": ref, "row": row, "excess": excess}


def record(db, picked: dict, task: int, now: float | None = None) -> None:
    """Remember the pass whose task is `task`."""
    state = db.kv(KEY, {}) or {}
    state["passes"] = (state.get("passes") or [])[-(KEEP_PASSES - 1):] + [
        {"at": time.time() if now is None else now, "subsystem": picked["subsystem"], "task": task,
         "ref": picked["ref"], "before": picked["row"], "excess": picked["excess"]}]
    db.set_kv(KEY, state)
