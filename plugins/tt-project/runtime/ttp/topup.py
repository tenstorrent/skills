# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Budget top-ups: an urgent or user-ordered task never waits on its per-task budget.

A task qualifies (eligible) when it is urgent (urgent.is_urgent: priority 1, a spec `urgent:` line) or
was created for a user's order (origin 'user', or it answers a chat: reply_chat). When dispatch finds
such a task with its budget spent, the daemon raises its budget by its tier's default
(budget.task_default_usd) and starts it in the same tick, with no coordinator turn
(Daemon._dispatch). A run of one stopped by its budget mid-run goes back to the queue, not to failed,
so the next tick tops it up and continues it (Daemon._finish_worker); a budget stop is either the
runner's check (run status 'budget') or the agent's own cap (a failed run, error_max_budget_usd:
turnsize.over_budget), recorded on the run (mark_stop) for the runaway guard. Each top-up is one KIND event on
the task (data: the budget before and after, the branch head, the count), handled at once so it wakes
no turn, and one low notice to the chat.

Hard limits stay: the dollar caps (the budget gate, _start_hold's 'caps') still hold or stop it, as
for any task. Runaway guard (runaway): when a run stopped by its budget follows a top-up that itself
followed a run stopped by its budget, with no hand-off and the branch head unchanged since that
top-up, the task ends failed with that evidence: it is never topped up forever nor held on budget.
Every other task keeps the plain budget hold (task_budget_exhausted). `budget.urgent_topup` false
turns this off (default on)."""
from __future__ import annotations

import json
import time

from . import urgent
from .budget import task_default_usd

KIND = "task_budget_topup"
SETTING = "urgent_topup"   # budget.urgent_topup
SPENT_LEFT = 0.05          # a task with this much or less left has spent its budget (_start_hold)


def enabled(cfg: dict) -> bool:
    return (cfg.get("budget") or {}).get(SETTING, True) is not False


def eligible(task: dict | None) -> bool:
    """Urgent, or created for a user's order (origin 'user', or it answers a chat)."""
    if not task:
        return False
    return urgent.is_urgent(task) or task.get("origin") == "user" or bool(task.get("reply_chat"))


def spent(task: dict) -> bool:
    return bool(task.get("budget_usd")) and \
        float(task["budget_usd"] or 0) - float(task.get("spent_usd") or 0) <= SPENT_LEFT


def events(db, tid: int) -> list[dict]:
    """The task's top-ups, oldest first, each with its data decoded."""
    out = []
    for r in db.q("SELECT id, ts, text, data FROM events WHERE kind=? AND task=? ORDER BY id", (KIND, tid)):
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            data = {}
        out.append({**r, "data": data if isinstance(data, dict) else {}})
    return out


def count(db, tid: int) -> int:
    return int(db.q("SELECT COUNT(*) n FROM events WHERE kind=? AND task=?", (KIND, tid))[0]["n"] or 0)


def top_up(db, cfg: dict, task: dict, head: str = "", now: float | None = None) -> dict:
    """Raise task's budget by its tier's default; record it (one event, one low notice). Returns the
    task with its new budget_usd."""
    now = time.time() if now is None else now
    step = float(task_default_usd(cfg, task.get("tier") or "standard"))
    before = float(task.get("budget_usd") or 0)
    after = round(max(before, float(task.get("spent_usd") or 0)) + step, 2)
    n = count(db, task["id"]) + 1
    db.update_task(task["id"], budget_usd=after)
    why = urgent.why(task) or "ordered by the user"
    text = (f"#{task['id']} {task['title'][:100]} ({why}) spent its ${before:.2f} budget: raised to ${after:.2f} "
            f"(top-up {n}, +${step:.2f}); the dollar caps still apply")
    db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (now, "daemon", KIND, "low", text,
          json.dumps({"before": before, "after": after, "step": step, "head": head, "n": n}), "handled", task["id"]))
    db.post("out", f"tt-project: budget top-up: {text}", chat=None, kind="info", severity="low")
    return {**task, "budget_usd": after}


STOP_KEY = "budget_stop"   # in a run's note: it ended on its budget (turnsize.over_budget)


def mark_stop(db, run: dict) -> None:
    """Record on the run that its budget stopped it, whichever way (the runner's check: status 'budget';
    the agent's own cap: a failed run with error_max_budget_usd), so runaway reads one cause."""
    if not run.get("id"):
        return
    row = db.one("SELECT note FROM runs WHERE id=?", (run["id"],))
    try:
        note = json.loads((row or {}).get("note") or "{}")
    except ValueError:
        note = {}
    if not isinstance(note, dict):
        note = {}
    db.x("UPDATE runs SET note=? WHERE id=?", (json.dumps({**note, STOP_KEY: True}), run["id"]))


def stopped_on_budget(row: dict) -> bool:
    if row.get("status") == "budget":
        return True
    try:
        note = json.loads(row.get("note") or "{}")
    except ValueError:
        return False
    return isinstance(note, dict) and note.get(STOP_KEY) is True


def runaway(db, task: dict, run: dict, head: str, handed_off: bool) -> str:
    """Evidence when `run`, which just stopped on its budget, repeats the run before the last top-up:
    that top-up came after a worker run stopped by its budget, `run` started after it, handed off
    nothing and the branch head is unchanged since. "" otherwise."""
    if handed_off or not run.get("id"):
        return ""
    ups = events(db, task["id"])
    if not ups:
        return ""
    last = ups[-1]
    prev = db.q("SELECT id, status, started, note FROM runs WHERE task=? AND role='worker' AND id<? "
                "ORDER BY id DESC LIMIT 1", (task["id"], run["id"]))
    if not prev or not stopped_on_budget(prev[0]) or float(prev[0]["started"] or 0) > last["ts"] \
            or float(run.get("started") or 0) < last["ts"]:
        return ""
    if (last["data"].get("head") or "") != (head or ""):
        return ""
    return (f"runaway guard: runs {prev[0]['id']} and {run['id']} both stopped on their budget with no "
            f"hand-off and no new commit (head {(head or 'none')[:12]}) across top-up {last['data'].get('n')} "
            f"(${float(last['data'].get('before') or 0):.2f} -> ${float(last['data'].get('after') or 0):.2f}); "
            f"not topped up again")


def digest_lines(db, since: float) -> list[str]:
    """Top-ups since the last coordinator turn, one line each."""
    return [f"- {r['text']}" for r in db.q("SELECT text FROM events WHERE kind=? AND ts>? ORDER BY id LIMIT 20",
                                            (KIND, since))]


def counts(db) -> dict[int, dict]:
    """Per task with top-ups: {"n": how many, "usd": dollars added}, one query."""
    out: dict[int, dict] = {}
    for r in db.q("SELECT task, data FROM events WHERE kind=? AND task IS NOT NULL", (KIND,)):
        try:
            step = float((json.loads(r["data"] or "{}") or {}).get("step") or 0)
        except (ValueError, TypeError, AttributeError):
            step = 0.0
        c = out.setdefault(int(r["task"]), {"n": 0, "usd": 0.0})
        c["n"] += 1
        c["usd"] = round(c["usd"] + step, 2)
    return out


def words(c: dict | None) -> str:
    """'budget topped up 2x (+$16.00)', or "" for none."""
    return f"budget topped up {c['n']}x (+${c['usd']:.2f})" if c and c.get("n") else ""
