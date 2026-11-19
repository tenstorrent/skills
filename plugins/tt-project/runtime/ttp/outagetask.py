# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Outage hand-offs as tracked work: a long down or held shared machine whose recovery owner cannot or
does not act gets a real task with a due time, never only a note.

The shared machine ledger (machine_ledger.route, model-free, in whichever co-tenant daemon ticks first)
decides, per condition past its grace that is neither vouched healthy nor quiet:

- the owner cannot act (machine_ledger.can_act: its daemon stopped or stale, the project paused, its
  coordinator logged out or failing): a fallback task at once;
- the owner can act but no recovery action is recorded within its budget (`heal.outage_owner_budget_min`,
  default 60, from the outage's start): the same task. A recovery action is a change-ledger entry on the
  machine opened since the outage began (`ttp machines change add`); a known outage (`ttp machines
  outage`) makes the condition quiet instead.

The task goes to the machine's recovery fallback when it can act, else to the first detecting project (a
co-tenant that reported the condition) that can, else to the fallback (or the first detector) anyway.
Each project creates it in its own database only: the daemon that routes it creates it itself when the
target is its own project, else files a daemon note to the target carrying an `outage` record, and the
target's daemon creates the task when it reads its inbox (upstream._file). No project writes another's
state. The task is labeled `outage:<alias>:<condition>@<start>` (one per episode, created once) and
`due:<epoch>` (`heal.outage_task_due_min`, default 30, after it was queued); its spec names the
machine, the outage start, who was paged and why the owner was passed over.

Escalation, from the same route: past its due time with the task still open (not closed(), so a failed
task counts; or unreadable), one high `machine_outage_overdue` page to the task's project (an event for a tricky turn when that is the routing daemon's
own project, else a high note), and after a second budget (`heal.outage_escalate_min`, default 30) one
normal-severity user alert. The task shows in `ttp status`, the web app and the blocker sweep as an
outage item with its age and due time (lines, sweep.items). Observation mutes never touch any of this:
the ledger reads only the condition reports, which a command watcher files before its own mute check.
"""
from __future__ import annotations

import json
import time
from typing import Any

LABEL = "outage:"   # task label: outage:<alias>:<condition>@<episode start>
DUE = "due:"        # task label: due:<epoch>
KIND = "machine_outage_task"          # the page that creates the task; the inbox note's `outage` record
OVERDUE_KIND = "machine_outage_overdue"   # the page (and event) for an open task past its due time
OWNER_BUDGET_S = 3600.0   # heal.outage_owner_budget_min: an owner that can act has this long to record an action
TASK_DUE_S = 1800.0       # heal.outage_task_due_min: a fallback task is due this long after it was queued
ESCALATE_S = 1800.0       # heal.outage_escalate_min: past due by this much, the user is alerted
CLOSED = ("done", "cancelled")   # closed(): no overdue page, not listed open; a failed task stays open
FIELDS = ("alias", "condition", "first", "due", "why", "tried", "seen_by", "text", "label")


KEYS = {"owner_s": ("outage_owner_budget_min", OWNER_BUDGET_S), "due_s": ("outage_task_due_min", TASK_DUE_S),
        "escalate_s": ("outage_escalate_min", ESCALATE_S)}
MAX_MIN = 7 * 24 * 60   # a week: the largest heal.outage_*_min


def minutes(key: str, v: Any) -> float:
    """heal.<key> (a KEYS name) as minutes, a number from 1 to MAX_MIN; ValueError otherwise."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= MAX_MIN:
        raise ValueError(f"heal.{key}: {v!r} is not a number of minutes from 1 to {MAX_MIN}; the default is used")
    return float(v)


def config_problems(raw: dict) -> list[str]:
    """One line per heal.outage_*_min in a project's own settings that budgets() ignores."""
    heal = raw.get("heal") if isinstance(raw.get("heal"), dict) else {}
    out = []
    for name, _ in KEYS.values():
        if name in heal:
            try:
                minutes(name, heal[name])
            except ValueError as e:
                out.append(str(e))
    return out


def budgets(cfg: dict | None) -> dict[str, float]:
    """{owner_s, due_s, escalate_s} in seconds from the project's `heal` settings (minutes); the default
    for one unset or invalid (config_problems)."""
    heal = (cfg or {}).get("heal") or {}
    out = {}
    for key, (name, default) in KEYS.items():
        try:
            out[key] = minutes(name, heal[name]) * 60
        except (KeyError, ValueError):
            out[key] = default
    return out


def closed(status: str | None) -> bool:
    """Whether an outage task in `status` is over: the one predicate for escalation (machine_ledger) and
    open_tasks (status, web, sweep). A failed task stays open, as nobody recovered the machine."""
    return status in CLOSED


def label(key: str, first: float) -> str:
    return f"{LABEL}{key}@{int(float(first))}"


def record(c: dict, key: str, why: str, tried: str, due: float) -> dict:
    """The `outage` record a KIND page carries: what the task's project needs to create it."""
    return {"alias": c["alias"], "condition": c["condition"], "first": float(c["first"]), "due": float(due),
            "why": why, "tried": tried, "seen_by": list(c.get("seen_by") or []),
            "text": str(c.get("text") or "")[:300], "label": label(key, c["first"])}


def valid(rec: Any) -> dict | None:
    """The record with only its known fields, or None when it is not one."""
    if not isinstance(rec, dict) or not all(rec.get(k) for k in ("alias", "condition", "label")):
        return None
    try:
        out = {k: rec.get(k) for k in FIELDS}
        out["first"], out["due"] = float(rec["first"]), float(rec["due"])
    except (KeyError, TypeError, ValueError):
        return None
    if not str(out["label"]).startswith(LABEL):
        return None
    out["seen_by"] = [str(s) for s in out.get("seen_by") or []][:10]
    return out


def find(db, lab: str) -> dict | None:
    return db.one("SELECT * FROM tasks WHERE labels LIKE ? ORDER BY id DESC LIMIT 1",
                  (f'%{json.dumps(lab)[1:-1]}"%',))


def ensure(db, rec: Any, where=None, now: float | None = None) -> int | None:
    """Queue this project's task for outage record `rec` once per episode; its id (new or existing),
    or None for a record that is not one."""
    from . import timefmt
    rec = valid(rec)
    if rec is None:
        return None
    with db.tx():
        t = find(db, rec["label"])
        if t:
            return int(t["id"])
        start, due = timefmt.long(rec["first"], where), timefmt.long(rec["due"], where)
        spec = (f"Outage hand-off: machine {rec['alias']} has been {rec['condition']} since {start}, reported by "
                f"{', '.join(rec['seen_by']) or 'a co-tenant'}: {rec.get('text') or 'no detail'}.\n"
                f"Why this project has it: {rec.get('why') or 'its recovery owner did not act'}.\n"
                f"Already tried: {rec.get('tried') or 'nothing recorded'}.\n"
                f"Due by {due}.\n\n"
                "Check the machine and recover it within the charter's restrictions (record every live change "
                "with `ttp machines change add`), or record a known outage with `ttp machines outage` and hand "
                "off naming who must act. If it already recovered, say so and finish. Still open past its due "
                "time, this task wakes the coordinator (at its configured effort, xhigh by default), then alerts the user.")
        return db.add_task(f"Outage: {rec['alias']} {rec['condition']} (recovery owner did not act)", spec,
                           kind="work", priority=1, origin="daemon", labels=[rec["label"], f"{DUE}{int(rec['due'])}"])


def _labels(t: dict) -> list[str]:
    try:
        got = json.loads(t.get("labels") or "[]")
    except ValueError:
        return []
    return [str(x) for x in got] if isinstance(got, list) else []


def info(t: dict) -> dict | None:
    """{label, alias, condition, first, due} of an outage task, else None."""
    labs = _labels(t)
    lab = next((x for x in labs if x.startswith(LABEL)), None)
    if not lab:
        return None
    body, _, first = lab[len(LABEL):].rpartition("@")
    alias, _, condition = body.rpartition(":")
    due = next((x[len(DUE):] for x in labs if x.startswith(DUE)), "")
    try:
        return {"label": lab, "alias": alias, "condition": condition, "first": float(first),
                "due": float(due) if due else None}
    except ValueError:
        return None


def open_tasks(db) -> list[dict]:
    """This project's open outage tasks, each a task row with its `outage` info."""
    out = []
    for t in db.q("SELECT * FROM tasks WHERE labels LIKE ? ORDER BY id", (f'%"{LABEL}%',)):
        i = info(t)
        if i and not closed(t["status"]):
            out.append({**t, "outage": i})
    return out


def line(t: dict, now: float, where=None) -> str:
    """'outage #12 box-a held for 1.5 h, task queued, due 10:30 PDT (OVERDUE 20 min)'."""
    from . import timefmt
    i = t["outage"]
    age = max(0.0, now - i["first"])
    out = f"outage #{t['id']} {i['alias']} {i['condition']} for {_dur(age)}, task {t['status']}"
    if i.get("due"):
        out += f", due {timefmt.short(i['due'], where, now)}"
        if now >= i["due"]:
            out += f" (OVERDUE {_dur(now - i['due'])})"
    return out


def lines(db, now: float | None = None, where=None) -> list[str]:
    now = time.time() if now is None else now
    return [line(t, now, where) for t in open_tasks(db)]


def _dur(s: float) -> str:
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{int(s // 60)} min"


def task_status(project: str, lab: str) -> str | None:
    """The status of the outage task labeled `lab` in local project `project`, read-only; None when
    there is none yet or its state cannot be read (another machine's project included)."""
    from . import globalcap, project as proj
    from pathlib import Path
    entry = (proj.load_registry().get("projects") or {}).get(project)
    if not isinstance(entry, dict) or not entry.get("dir") or entry.get("host") not in (None, proj.hostname()):
        return None
    db = Path(entry["dir"]) / proj.FOLDER / "state" / "project.db"
    if not db.is_file():
        return None
    try:
        conn = globalcap.connect_ro(db)
    except Exception:
        return None
    try:
        row = conn.execute("SELECT status FROM tasks WHERE labels LIKE ? ORDER BY id DESC LIMIT 1",
                           (f'%{json.dumps(lab)[1:-1]}"%',)).fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        conn.close()
