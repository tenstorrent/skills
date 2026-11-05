"""A newer user order on a machine or resource supersedes older tasks' conflicting steps there.

When the coordinator adds or updates a task for a user's order (its `user_msg` field, or a spec citing
a user message of the turn) and the task names resources, each becomes that resource's latest user
order, timed at the user's message. While the order's task is open, every other open task created or
last re-specified before that message is held there, model-free:
- `ttp lock <resource>` (and its probe) refuses it, exit 75, naming the order;
- dispatch keeps it back (hold kind "order"), so the order's task gets the resource first;
- running ones get one steer.md update naming the order;
- the coordinator's digest lists them, to re-spec or cancel that turn.
A spec update by the coordinator after the message clears a task (it saw the order then).
"""
from __future__ import annotations

import time

ORDERS_KEY = "user_orders"            # kv: {resource: {"task", "msg", "ts", "at"}}, the latest per resource
RESPECS_KEY = "user_order_respecs"    # kv: {task id: when the coordinator last changed its spec}
TERMINAL = ("done", "failed", "cancelled")
RESPECS_KEPT = 500


def same(cfg: dict | None, a: str, b: str) -> bool:
    """Whether two resource names meet: one lock (device aliases share one), or one is the machine of
    the other (`<alias>` and `<alias>-device`)."""
    from . import locks
    if not a or not b:
        return False
    if a == b or (cfg is not None and locks.canonical(cfg, a) == locks.canonical(cfg, b)):
        return True
    return any(y.startswith((x + "-", x + ":")) for x, y in ((a, b), (b, a)))


def _orders(db) -> dict:
    got = db.kv(ORDERS_KEY, {}) or {}
    return got if isinstance(got, dict) else {}


def _respecs(db) -> dict:
    got = db.kv(RESPECS_KEY, {}) or {}
    return got if isinstance(got, dict) else {}


def cited(db, text: str, messages: list[int] | None) -> int | None:
    """The newest user message of this turn (`messages`) that `text` cites, else None."""
    from .coordinator import _user_msg_ids
    ids = {int(i) for i in _user_msg_ids(text or "")} & {int(m) for m in messages or []}
    return max(ids) if ids else None


def record(db, task_id: int, msg_id: int, resources, now: float | None = None) -> list[str]:
    """Make task `task_id` the latest user order on each of `resources`, timed at message `msg_id`;
    an order from an older message never replaces a newer one. Returns the resources it now orders."""
    now = time.time() if now is None else now
    row = db.one("SELECT ts FROM messages WHERE id=? AND direction='in'", (int(msg_id),))
    ts = float(row["ts"]) if row and row["ts"] else now
    if not _open(db, task_id):
        return []   # no open task to carry the order
    orders = _orders(db)
    done = []
    for res in sorted({str(r) for r in resources or [] if r}):
        prev = orders.get(res)
        if prev and int(prev.get("task") or 0) != int(task_id) and (float(prev.get("ts") or 0) > ts or (
                int(prev.get("msg") or 0) == int(msg_id) and _open(db, prev.get("task")))):
            continue   # a newer order stands, and so does the first open task of the same order
        orders[res] = {"task": int(task_id), "msg": int(msg_id), "ts": ts, "at": now}
        done.append(res)
    if done:
        live = {r: o for r, o in orders.items() if _open(db, o.get("task"))}
        db.set_kv(ORDERS_KEY, live)
    return done


def respec(db, task_id: int, now: float | None = None) -> None:
    """The coordinator changed task `task_id`'s spec now: it was re-specified after any earlier order."""
    marks = _respecs(db)
    marks[str(int(task_id))] = time.time() if now is None else now
    if len(marks) > RESPECS_KEPT:
        marks = dict(sorted(marks.items(), key=lambda kv: kv[1])[-RESPECS_KEPT:])
    db.set_kv(RESPECS_KEY, marks)


def _open(db, task_id) -> dict | None:
    try:
        t = db.task(int(task_id))
    except (TypeError, ValueError):
        return None
    return t if t and t["status"] not in TERMINAL else None


def active(db) -> dict:
    """{resource: order} of the orders whose task is still open."""
    return {r: o for r, o in _orders(db).items() if _open(db, o.get("task"))}


def predates(db, task: dict, order: dict, respecs: dict | None = None) -> bool:
    """Whether `task` (not the order's own) was created and last re-specified before the order."""
    if not task or int(task["id"]) == int(order.get("task") or 0) or task.get("status") in TERMINAL:
        return False
    marks = _respecs(db) if respecs is None else respecs
    stamp = max(float(task.get("created") or 0), float(marks.get(str(int(task["id"]))) or 0))
    return stamp < float(order.get("ts") or 0)


def holding(db, cfg: dict | None, task: dict | None, names) -> tuple[str, str, dict] | None:
    """(name asked, ordered resource, order) of the first newer user order that holds `task` off one
    of `names`; None when none does."""
    if not task:
        return None
    orders = active(db)
    if not orders:
        return None
    marks = _respecs(db)
    for name in names:
        for res, o in sorted(orders.items()):
            if same(cfg, name, res) and predates(db, task, o, marks):
                return name, res, o
    return None


def line(p, task_id, name: str, res: str, order: dict) -> str:
    """Why task `task_id` may not use `name` now."""
    from . import timefmt
    when = timefmt.short(float(order["ts"]), p) if p is not None else ""
    return (f"{name}: user msg #{order['msg']}{f' ({when})' if when else ''} ordered task #{order['task']} on "
            f"{res}, newer than task #{task_id}'s spec: its steps on {res} are superseded until the coordinator "
            f"re-specifies it or #{order['task']} ends; hand it back as waiting")


def steer_running(p, ordered: list[str], order_task: int) -> list[int]:
    """Tell each running run of another task that names an ordered resource and predates the order,
    once, through its steer.md, to stop conflicting steps there. Returns the task ids told."""
    from pathlib import Path

    from . import coordinator as coord
    from . import timefmt
    db, cfg = p.db, p.config()
    orders = active(db)
    told = []
    for r in db.q("SELECT id, task, dir FROM runs WHERE status='running' AND role!='coordinator' "
                  "AND task IS NOT NULL AND dir IS NOT NULL"):
        t = db.task(r["task"])
        if not t:
            continue
        for res in ordered:
            o = orders.get(res)
            if not o or int(o["task"]) != int(order_task) or not predates(db, t, o):
                continue
            if not any(same(cfg, mine, res) for mine in coord.task_resources(t)):
                continue
            text = (f"Newer user order: user msg #{o['msg']} ({timefmt.short(float(o['ts']), p)}) ordered task "
                    f"#{o['task']} on {res}, after this task's spec. Stop now any step on {res} that conflicts "
                    f"with it (restarting or continuing a job, a reset, a reboot, a reservation) and start none: "
                    f"`ttp lock {res}` refuses this task until the coordinator re-specifies it or #{o['task']} "
                    f"ends. Commit, then hand off `waiting` with retry_when `ttp lock --probe {res}` if the rest "
                    f"needs {res}.")
            coord._append_update(Path(r["dir"]) / "steer.md", text, f"user-order-{res}-{o['msg']}-{r['id']}")
            told.append(int(t["id"]))
            break
    return told


def digest_lines(p) -> list[str]:
    """The coordinator's digest section: each open newer order and the open tasks that predate it there."""
    from . import timefmt
    from .coordinator import task_resources
    db, cfg = p.db, p.config()
    orders = active(db)
    if not orders:
        return []
    marks = _respecs(db)
    rows = db.q("SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled') ORDER BY id")
    out = []
    for res, o in sorted(orders.items()):
        older = [t for t in rows if predates(db, t, o, marks)
                 and any(same(cfg, mine, res) for mine in task_resources(t))]
        if older:
            out.append(f"- {res}: user msg #{o['msg']} ({timefmt.short(float(o['ts']), p)}), task #{o['task']}. "
                       "Older: " + ", ".join(f"#{t['id']} ({t['status']}) {str(t['title'])[:60]}" for t in older))
    if not out:
        return []
    return ["## Newer user orders on a resource (the older open tasks there are held: `ttp lock` refuses "
            "them and dispatch keeps them back. This turn, cancel each or re-spec it to fit the order)"] + out
