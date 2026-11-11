"""Must-act wakes: the coordinator follows up on what it owes without being asked.

An overdue promise or order, an order whose owner never started and a claim whose check failed
(obligations.MUST_ACT_KINDS) each raise one high event whose data carries `must_act` (the item's key,
chat and owner tasks). Such an event wakes the coordinator at once, past the batching holds, and the
turn's prompt opens with block(): one line per item that this turn must act on. After the turn,
settle() checks each item got an applied action naming it (acted): a task_add or task_update naming
its key in a text field, a task_update of an owner (or a task_add continuing one), an `obligation`
action with its key, a reply to its chat, or a notify (it reaches every chat). An item left untouched
is raised once more (REWAKE_KIND, `<fingerprint>:rewake`), which wakes the coordinator again at once;
left untouched again, the daemon posts a model-free update in its chat from the records (what is late,
its owners and their last progress) and raises a daemon issue. Every step is keyed by the item's
event fingerprint, which names its state (its due time), so the same state never wakes twice.
The same block() and settle() serve a persistent coordinator session: each wake is one turn."""

from __future__ import annotations

import json
import re
import time

from . import obligations

REWAKE_KIND = "must_act_rewake"
QUEUE_KIND = "queue_wait"   # queuewait.KIND: a wait on a queue or cap past its threshold, re-raised per mark
KINDS = (*obligations.MUST_ACT_KINDS, REWAKE_KIND, QUEUE_KIND)
KV = "must_act"                 # {"told": {fingerprint: at}}: the model-free updates already posted
TOLD_KEPT = 200
SECTION = "Must act now"
LATE = {obligations.PROMISE_KIND: "promise overdue", obligations.ORDER_KIND: "order overdue",
        obligations.ORDER_LATE_KIND: "order not started", obligations.CLAIM_KIND: "claim does not hold",
        obligations.LANDING_STUCK_KIND: "stuck behind a landing nothing can make",
        QUEUE_KIND: "queue wait"}


def item(ev) -> dict | None:
    try:
        return (json.loads(ev["data"] or "{}") or {}).get("must_act")
    except (TypeError, ValueError, AttributeError):
        return None


def events(db, ids=None) -> list:
    """The queued must-act events, or those among `ids` (any status)."""
    q = ",".join("?" * len(KINDS))
    if ids is None:
        return db.q(f"SELECT * FROM events WHERE status='queued' AND kind IN ({q}) ORDER BY id", KINDS)
    ids = [int(i) for i in ids]
    if not ids:
        return []
    return db.q(f"SELECT * FROM events WHERE kind IN ({q}) AND id IN ({','.join('?' * len(ids))}) ORDER BY id",
                (*KINDS, *ids))


def queued(db) -> bool:
    return bool(events(db))


def block(db, ids) -> str:
    """The prompt section naming each must-act item among events `ids`; '' with none."""
    out = []
    for ev in events(db, ids):
        it = item(ev)
        if not it:
            continue
        first = (ev["text"] or "").split("\n", 1)[0][:300]
        again = " (second wake: you did not act on it last turn)" if ev["kind"] == REWAKE_KIND else ""
        again += " (re-plan: missed twice)" if it.get("replan") else ""
        if ev["kind"] == QUEUE_KIND:
            again = (" (queue wait: queue its fix now (config_set, task_add or task_update naming the key or "
                     "updating the task), or a noop whose `why` names the key and the safety rule that holds it; "
                     "else it "
                     "comes back at the next mark)")
        owners = f"; owners #{', #'.join(map(str, it.get('tasks') or []))}" if it.get("tasks") else "; no owner task"
        out.append(f"- {it['key']} in chat {it['chat']}{owners}{again}: {first}")
    if not out:
        return ""
    return (f"## {SECTION}\nEach item below is late or did not hold. Act on every one in this turn: re-order or start its "
            "work (task_add or task_update naming its key, or updating its owner), record or close it "
            "(`obligation` with its key), or a noop whose `why` names its key and what is true. A chat reply "
            "only for a result, an outage or a blocker only the user can clear: routine updates wait for the "
            "chat's digest. An item with no such action wakes you once more, then goes in its chat's digest.\n"
            + "\n".join(out))


def _names(text, key: str) -> bool:
    return bool(re.search(rf"(?<![\w./-]){re.escape(key)}(?![\w-]|\.\d)", str(text or "")))


def _id(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def acted(it: dict, actions: list, applied) -> str | None:
    """How an applied action of the turn acted on must-act item `it`; None when none did."""
    key, chat, owners = it["key"], obligations._chat(it.get("chat")), set(it.get("tasks") or [])
    queue = it.get("kind") == QUEUE_KIND
    for i in sorted(applied or ()):
        a = actions[i] if 0 <= i < len(actions) else None
        if not isinstance(a, dict):
            continue
        t = a.get("type")
        if t in ("task_add", "task_update"):
            if any(_names(v, key) for v in a.values() if isinstance(v, str)):
                return f"{t} names {key}"
            if t == "task_update" and _id(a.get("id")) in owners:
                return f"task_update of owner #{a['id']}"
            if t == "task_add" and _id(a.get("continues")) in owners:
                return f"task_add continues owner #{a['continues']}"
        elif t == "obligation" and str(a.get("key") or "").strip() == key:
            return f"obligation action on {key}"
        elif t == "noop" and any(_names(v, key) for v in a.values() if isinstance(v, str)):
            return f"noop names {key}"
        elif queue:   # any config change, or words naming the item (the rule that holds it)
            if t == "config_set":
                return "config_set"
            if t in ("reply", "notify") and any(_names(v, key) for v in a.values() if isinstance(v, str)):
                return f"{t} names {key}"
        elif t == "reply" and (chat == "all" or obligations._chat(a.get("chat")) in (chat, "all")):
            return f"reply to chat {chat}"
        elif t == "notify":
            return "notify"
    return None


def _status(p, it: dict) -> str:
    from . import timefmt
    db = p.db
    o = obligations.get(db, it["key"]) or {}
    lines = [f"{LATE.get(it.get('kind'), 'late')}: {o.get('text') or it['key']}"]
    pr = o.get("promise") or {}
    if pr.get("due"):
        lines[0] += f" (due {timefmt.long(pr['due'], p)})"
    ids = it.get("tasks") or []
    for t in db.q(f"SELECT id, title, status, blocked_reason, updated FROM tasks WHERE id IN "
                  f"({','.join('?' * len(ids))})", ids) if ids else []:
        run = db.one("SELECT started, ended, status FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (t["id"],))
        last = (f"last run {run['status']}, started {timefmt.long(run['started'], p)}" if run and run["started"]
                else "no run started yet")
        why = f"; {str(t['blocked_reason'])[:120]}" if t["blocked_reason"] and t["status"] != "done" else ""
        lines.append(f"Owner #{t['id']} {str(t['title'])[:80]}: {t['status']}{why}; {last}; "
                     f"last change {timefmt.long(t['updated'], p)}")
    if not ids:
        lines.append("Owner: no task owns it yet")
    return "\n".join(lines)


def settle(p, ids, actions, applied, now: float | None = None) -> list[tuple[str, str]]:
    """After a turn that handled events `ids` and applied `applied` (indexes into `actions`): an item no
    action touched is raised once more, or, the second time, told to its chat model-free. Returns
    (issue key, issue text) per item told, for the daemon to raise."""
    now = time.time() if now is None else now
    db = p.db
    issues: list[tuple[str, str]] = []
    for ev in events(db, ids):
        it = item(ev)
        if not it or not isinstance(actions, list):
            continue
        if it.get("kind") == QUEUE_KIND:   # acted: no more raises for this wait; else the next mark raises it
            if acted(it, actions, applied):
                from . import queuewait
                queuewait.record_act(db, str(it.get("episode") or ""), now,
                                     queuewait.fix_owner(db, it, actions, applied))
            continue
        if acted(it, actions, applied):
            continue
        o = obligations.get(db, it["key"])
        if o and not obligations._open(o):
            continue   # closed meanwhile (kept, dropped): nothing is owed
        fp = ev["fingerprint"]
        if ev["kind"] != REWAKE_KIND:
            data = {"must_act": {**it, "of": fp, "kind": ev["kind"]}}
            obligations._raise(db, now, f"{fp}:rewake", REWAKE_KIND,
                               f"Not acted on last turn. {ev['text'] or ''}", ev["task"], data)
            continue
        first = str(it.get("of") or fp)
        with db.tx():
            st = db.kv(KV) or {}
            told = st.setdefault("told", {})
            if first in told:
                continue
            told[first] = now
            if len(told) > TOLD_KEPT:
                for k in sorted(told, key=told.get)[:len(told) - TOLD_KEPT]:
                    del told[k]
            db.set_kv(KV, st)
            text = (f"Automatic update from the project's records (the coordinator did not act on this in two "
                    f"turns): {_status(p, it)}")
            chat = obligations._chat(it.get("chat"))
        from . import chatdigest   # routine: the chat's next digest carries it
        heard = ("its chat's next digest carries the update from the records"
                if chatdigest.routine(p, chat, text, "must-act not acted on", now) else "its chat was told model-free")
        issues.append((f"must-act:{first}", f"must-act item {it['key']} ({LATE.get(it.get('kind'), 'late')}) "
                                             f"got no action in two coordinator turns; {heard}. Event {first}."))
    return issues
