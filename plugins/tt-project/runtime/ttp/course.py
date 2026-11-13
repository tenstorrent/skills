# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Course check: model-free signs, in the coordinator's digest, that the project is off track or
working inefficiently, each with its evidence and a suggested correction.

The section is omitted when nothing trips, so a healthy project pays no tokens for it. It reads only
the project's database (tasks, runs, their spend and the coordinator turns' notes). Conditions, each
with a threshold under `coordinator.course_check` in project.json (0 turns one off):

- fails: an open task with `fails_24h` (3) or more failed, lost, timed-out or stalled worker runs
  (a reboot's lost run is not counted) or `task_failed` events in 24 h. The effort trigger at
  `coordinator.repeat_fails_24h` only raises a turn; this line names the task and its cost.
- queued: free worker slots while queued tasks that never ran have waited `queued_h` (6) hours
  or more on an unfinished dependency or a paused resource. Planned deferrals are left out: the
  blocker sweep covers them.
- spend: a task of one kind open or finished in the last 24 h that spent `spend_ratio` (3) times
  or more the project's 7-day median per finished task of that kind, and at least `spend_min_usd`
  ($2); the median needs `spend_min_samples` (5) finished tasks. At most COSTLY_SHOWN lines, open
  tasks first, then the costliest finished ones, in id order; one count line names the rest and
  `ttp course <project>`, which lists them all.
- idle_turns: the last `idle_turns` (6) or more coordinator turns in a row decided nothing, with
  their cost and what woke them.

Left to the sections that already cover them: stuck, waiting, deferred and in-review tasks and asks
held on the user (Blocker sweep, ask re-think), review fix loops across stacks (review_area_cap),
continue chains that stall (continue_cap), repeats of tried work (Tried work; task_add refuses
them), idle devices (idle_capacity), noisy watchers and failing resources (Resource trouble)."""
from __future__ import annotations

import json
import statistics
import time
from typing import Any

from .db import DB, TERMINAL_TASK_STATES, deferral

KEY = "course_check"
HEADER = "## Course check (correct course this turn, or say in summary why it is fine)"
DEFAULTS = {"fails_24h": 3, "queued_h": 6, "spend_ratio": 3, "spend_min_usd": 2, "spend_min_samples": 5,
            "idle_turns": 6}
BAD_RUNS = ("failed", "lost", "timeout", "stalled")
DAY = 86400
TITLE_CHARS = 60
COSTLY_SHOWN = 10   # the 'costly:' lines a digest shows; the rest are one count line


def settings(cfg: dict | None) -> dict:
    got = ((cfg or {}).get("coordinator") or {}).get(KEY) or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        try:
            out[k] = max(0.0, float(got.get(k, DEFAULTS[k]) or 0)) if isinstance(got, dict) else DEFAULTS[k]
        except (TypeError, ValueError):
            pass
    return out


def _title(t: dict) -> str:
    s = " ".join(str(t.get("title") or "").split())
    return s if len(s) <= TITLE_CHARS else s[:TITLE_CHARS - 1] + "…"


def _ids(rows: list[dict], n: int = 6) -> str:
    ids = ", ".join(f"#{r['id']}" for r in rows[:n])
    return ids + (f" and {len(rows) - n} more" if len(rows) > n else "")


def fails(db: DB, at: float, now: float) -> list[dict]:
    if not at:
        return []
    since, out = now - DAY, []
    for t in db.q(f"SELECT id, title, spent_usd FROM tasks WHERE status NOT IN ({','.join('?' * len(TERMINAL_TASK_STATES))}) "
                  "ORDER BY id", TERMINAL_TASK_STATES):
        runs = db.one("SELECT COUNT(*) n, COALESCE(SUM(cost_usd), 0) usd FROM runs WHERE task=? AND role!='coordinator' "
                      f"AND ended>? AND status IN ({','.join('?' * len(BAD_RUNS))}) "
                      "AND COALESCE(note,'') NOT LIKE '%lost_to_reboot%'", (t["id"], since, *BAD_RUNS))
        events = db.one("SELECT COUNT(*) n FROM events WHERE task=? AND kind='task_failed' AND ts>?", (t["id"], since))["n"]
        n = max(runs["n"], events)
        if n >= at:
            out.append({"id": t["id"], "title": _title(t), "n": n, "usd": float(runs["usd"] or 0)})
    return out


def queued(db: DB, cfg: dict, hours: float, now: float) -> dict | None:
    """Queued tasks that never ran and waited `hours` on a dependency or a paused resource, while a
    worker slot is free."""
    if not hours:
        return None
    slots = int(((cfg or {}).get("budget") or {}).get("max_parallel_workers", 6) or 0)
    running = db.one("SELECT COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running'")["n"]
    if running >= slots:
        return None
    rows = db.q("SELECT * FROM tasks WHERE status='queued' AND created<=? AND NOT EXISTS "
                "(SELECT 1 FROM runs WHERE runs.task=tasks.id) ORDER BY created", (now - hours * 3600,))
    if not rows:
        return None
    from .coordinator import task_resources
    paused, unmet = db.paused_resources(), db.unmet_dependencies(rows)
    held = []
    for t in rows:
        d = deferral(t)
        if "when" in d or float(d.get("after") or 0) > now or float(t.get("not_before") or 0) > now:
            continue   # a planned deferral: the blocker sweep covers it
        why = [f"paused {r}" for r in sorted(task_resources(t) & paused.keys())]
        why += [f"dependency #{x}" for x in unmet[t["id"]]]
        if why:
            held.append({**t, "why": why})
    if not held:
        return None
    return {"tasks": held, "age_h": (now - float(held[0]["created"] or now)) / 3600,
            "free": slots - running, "slots": slots,
            "why": sorted({w for t in held for w in t["why"]})}


def spend(db: DB, ratio: float, floor: float, samples: float, now: float) -> list[dict]:
    if not ratio:
        return []
    out = []
    week = db.q("SELECT kind, spent_usd FROM tasks WHERE status='done' AND updated>=? AND spent_usd>0", (now - 7 * DAY,))
    by_kind: dict[str, list[float]] = {}
    for r in week:
        by_kind.setdefault(r["kind"], []).append(float(r["spent_usd"]))
    for kind, costs in sorted(by_kind.items()):
        if len(costs) < max(1, samples):
            continue
        median = statistics.median(costs)
        line = max(ratio * median, floor)
        for t in db.q("SELECT id, title, status, spent_usd FROM tasks WHERE kind=? AND spent_usd>=? AND "
                      f"(status NOT IN ({','.join('?' * len(TERMINAL_TASK_STATES))}) OR updated>=?) ORDER BY spent_usd DESC",
                      (kind, line, *TERMINAL_TASK_STATES, now - DAY)):
            out.append({"id": t["id"], "title": _title(t), "status": t["status"], "kind": kind,
                        "usd": float(t["spent_usd"]), "median": median, "x": float(t["spent_usd"]) / median})
    return out


def _note(raw: Any) -> dict:
    try:
        note = json.loads(raw or "{}")
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def idle_turns(db: DB, at: float) -> dict | None:
    """The newest coordinator turns in a row that decided nothing (their note's `decided` is 0)."""
    if not at:
        return None
    streak, wakes = [], {}
    for r in db.q("SELECT started, cost_usd, note FROM runs WHERE role='coordinator' AND status='ok' "
                  "ORDER BY started DESC LIMIT 200"):
        note = _note(r["note"])
        if "decided" not in note or note["decided"]:
            break
        streak.append(r)
        why = "message" if note.get("messages") else "events" if note.get("events") else note.get("wake_due") or "other"
        wakes[why] = wakes.get(why, 0) + 1
    if len(streak) < at:
        return None
    return {"n": len(streak), "usd": sum(float(r["cost_usd"] or 0) for r in streak),
            "hours": (float(streak[0]["started"] or 0) - float(streak[-1]["started"] or 0)) / 3600, "wakes": wakes}


def _costly(rows: list[dict], cap: int | None) -> tuple[list[dict], list[dict]]:
    """(the rows shown, the rest): open tasks first, then the costliest finished ones, shown in id order
    so a kept row keeps its place."""
    if cap is None or len(rows) <= cap:
        return sorted(rows, key=lambda t: t["id"]), []
    ranked = sorted(rows, key=lambda t: (t["status"] in TERMINAL_TASK_STATES, -t["usd"], t["id"]))
    return sorted(ranked[:cap], key=lambda t: t["id"]), ranked[cap:]


def digest_lines(db: DB, cfg: dict | None, now: float | None = None,
                 costly_cap: int | None = COSTLY_SHOWN) -> tuple[list[str], str]:
    """The section's lines ([] when nothing trips) and its identity for collapsing (conditions and
    task ids, no counts or clocks). `costly_cap`: the 'costly:' lines shown (None: all)."""
    now = time.time() if now is None else now
    s = settings(cfg)
    lines, ident = [], []
    for t in fails(db, s["fails_24h"], now):
        lines.append(f"- failing: #{t['id']} '{t['title']}' failed {t['n']} times in 24 h (${t['usd']:.2f}) | "
                     "change the approach or scope it down; do not run it again as is")
        ident.append(f"fails:{t['id']}")
    if q := queued(db, cfg or {}, s["queued_h"], now):
        lines.append(f"- queued work held: {_ids(q['tasks'])} never ran, oldest {q['age_h']:.1f}h, held by "
                     f"{', '.join(q['why'][:6])}; {q['free']} of {q['slots']} worker slots free | "
                     "unblock or drop the dependency, route around the resource, or cancel")
        ident.append("queued:" + ",".join(str(t["id"]) for t in q["tasks"]))
    shown, rest = _costly(spend(db, s["spend_ratio"], s["spend_min_usd"], s["spend_min_samples"], now), costly_cap)
    for t in shown:
        lines.append(f"- costly: #{t['id']} '{t['title']}' ({t['status']}) spent ${t['usd']:.2f}, {t['x']:.1f}x the "
                     f"7-day median ${t['median']:.2f} per done {t['kind']} task | "
                     + ("check it converges (not retrying the same step); if not, lower its tier or stop it"
                        if t["status"] not in TERMINAL_TASK_STATES
                        else "find why (retries, scope, tier) before queuing more like it"))
        ident.append(f"spend:{t['id']}")
    if rest:
        n_open = sum(1 for t in rest if t["status"] not in TERMINAL_TASK_STATES)
        lines.append(f"- costly: {len(rest)} more not listed ({n_open} open, {len(rest) - n_open} finished in 24 h; "
                     f"${sum(t['usd'] for t in rest):.2f}) | list them all: "
                     f"`ttp course {(cfg or {}).get('name') or '<project>'}`")
    if i := idle_turns(db, s["idle_turns"]):
        woke = ", ".join(f"{k} {v}" for k, v in sorted(i["wakes"].items(), key=lambda kv: (-kv[1], kv[0])))
        lines.append(f"- idle turns: the last {i['n']} coordinator turns decided nothing (${i['usd']:.2f} over "
                     f"{i['hours']:.1f}h; woken by {woke}) | mute or fix what keeps waking you, or queue charter work")
        ident.append("idle_turns")
    return ([HEADER, *lines] if lines else []), "\n".join(ident)
