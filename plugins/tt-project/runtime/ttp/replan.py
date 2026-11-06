"""A missed promise changes the plan, never only its time.

Each open promise, order or claim (obligations.py) keeps its `misses` (a due that passed with nothing
delivered) and `moves` (each later due, with the plan change or the user's message behind it). At
action apply, a due move on an obligation whose current due has passed is refused unless the same batch
changes the plan on its critical path (plan_change): a task_add naming it or continuing an owner, a
task_update of an owner changing priority, resources, depends_on, status or spec, an owner attached, a
config_set on a key the obligation or its owners name (the bottleneck), or a `justify` naming what
changed. A move citing the user's own message, sent in its chat after its due was set, is never refused. A reply giving a new time in the chat of
a missed promise with no plan change is flagged (flag_reply). A second miss makes the overdue item a
must-act re-plan (REPLAN_TEXT). audit_line is the daily review's metric. Model-free throughout, and the
same for session and stateless coordinators on every provider: all of it runs in coordinator.apply and
obligations.check."""

from __future__ import annotations

import re
import statistics
import time

FLAG_KIND = "promise_moved_unchanged"
PLAN_FIELDS = ("priority", "resources", "depends_on", "status", "spec")
PLAN_COLS = {"priority": "priority", "resources": "labels", "depends_on": "depends_on", "status": "status",
             "spec": "spec"}   # the task column each plan field lands in (resources ride in labels)
JUSTIFY_MIN_WORDS = 3
KEPT = 20   # misses and moves kept per obligation
REPLAN_TEXT = ("\nMissed twice: re-plan now (a new split, route or owner: task_add naming it, or task_update of its "
               "owner) or tell its chat honestly what is true, with any time from Expected finish's measured medians "
               "(`ttp eta {key}`), never a guessed or padded time. A new time alone is refused.")


def _names(text, key: str) -> bool:
    return bool(re.search(rf"(?<![\w./-]){re.escape(key)}(?![\w-]|\.\d)", str(text or "")))


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def note_miss(pr: dict, now: float) -> int:
    """Record that promise record `pr` missed its current due (once per due). Returns its misses so far."""
    misses = pr.setdefault("misses", [])
    if not any(float(m["due"]) == float(pr["due"]) for m in misses):
        misses.append({"due": float(pr["due"]), "at": now})
        del misses[:-KEPT]
    return len(misses)


def missed(pr: dict | None, now: float) -> bool:
    """Whether promise record `pr` is past its current due."""
    return bool(pr) and now >= float(pr["due"])


def note_move(pr: dict, to: float, now: float, plan: str | None, user: int | None, via: str) -> None:
    moves = pr.setdefault("moves", [])
    moves.append({"at": now, "from": float(pr["due"]), "to": float(to), "missed": missed(pr, now),
                  "plan": plan, "user": user, "via": via})
    del moves[:-KEPT]


def refusal(o: dict, due_text: str) -> str:
    owners = sorted({int(t) for t in o.get("owners") or {}} | set((o.get("promise") or {}).get("tasks") or []))
    own = ", ".join(f"#{t}" for t in owners) or "its owner"
    return (f"{o['id']} missed its due ({due_text}): a new time alone is refused. Change the plan on its critical "
            f"path in the same batch: a task_add naming {o['id']} (new owner, split or continue), a task_update of "
            f"{own} changing priority, resources, depends_on, status or spec (a new approach), an `obligation` "
            f"with key {o['id']} and new `owners`, a config_set on the bottleneck it waits on, or `justify` naming "
            f"what changed; or cite the user's message asking for the move (`message`). Else tell its chat what is "
            f"true, with a time from Expected finish (`ttp eta {o['id']}`).")


def user_asked(db, o: dict, mid: int) -> str | None:
    """Why the user's message `mid` cannot ask for a move of `o`'s due (None when it can): it must be in the
    promise's chat and come after its current due was set (set_at, else created). An older message, the
    order's own included, never asked for this move."""
    from . import obligations
    pr = o.get("promise") or {}
    row = db.one("SELECT ts, chat FROM messages WHERE id=?", (int(mid),))
    if not row:
        return f"#{mid} is not a message"
    if obligations._chat(row["chat"]) != pr.get("chat"):
        return f"#{mid} is in chat {obligations._chat(row['chat'])}, not the promise's chat {pr.get('chat')}"
    if float(row["ts"]) <= float(pr.get("set_at") or o.get("created") or 0):
        return f"#{mid} came before its current due was set, so it did not ask for this move"
    return None


def snapshot(db, actions: list) -> dict[int, dict]:
    """The plan fields (PLAN_COLS) of each task a task_update in `actions` names, read before the batch
    applies: plan_change compares them with the task after it."""
    out = {}
    for a in actions:
        if isinstance(a, dict) and a.get("type") == "task_update" and (tid := _int(a.get("id"))) is not None \
                and tid not in out and (t := db.task(tid)):
            out[tid] = {f: t[c] for f, c in PLAN_COLS.items()}
    return out


def _changed(db, a: dict, before: dict[int, dict] | None) -> list[str]:
    """The plan fields task_update `a` changes. With `before` (snapshot, the batch applied): those it names
    whose column differs now; without (not applied): those whose value differs from the task's. A spec
    update is appended to the spec, so it counts only when its text is not in the spec already."""
    named = [f for f in PLAN_FIELDS if a.get(f) not in (None, "")]
    t = db.task(int(a["id"])) if named else None
    if not t:
        return []
    was = before.get(int(a["id"])) if before is not None else {f: t[c] for f, c in PLAN_COLS.items()}
    if was is None:
        return []
    named = [f for f in named if f != "spec" or str(a["spec"]).strip() not in str(was["spec"] or "")]
    if before is not None:
        return [f for f in named if was[f] != t[PLAN_COLS[f]]]
    return [f for f in named if str(a[f]) != str(was[f])]


def _bottleneck_text(db, o: dict, owners: set[int]) -> str:
    parts = [o.get("text") or ""] + [str(j.get("why") or "") for j in (o.get("eta_justified") or {}).values()]
    for tid in owners:
        t = db.task(tid)
        if t:
            parts += [t["blocked_reason"] or "", t["labels"] or ""]
    return "\n".join(parts)


def plan_change(db, o: dict, actions: list, skip=(), since: float | None = None,
                before: dict[int, dict] | None = None) -> str | None:
    """What in `actions` (indexes in `skip` were refused) changes obligation `o`'s plan; None when nothing.
    `since`: when the batch started applying; an owner attached from then on is new. `before`: owners'
    plan fields before the batch (snapshot); a task_update setting a field to its value is no change."""
    key = o["id"]
    owners = {int(t) for t in o.get("owners") or {}} | set((o.get("promise") or {}).get("tasks") or [])
    neck = None
    for i, a in enumerate(actions):
        if i in skip or not isinstance(a, dict):
            continue
        t = a.get("type")
        if t == "task_add":
            if any(_names(v, key) for v in a.values() if isinstance(v, str)):
                return f"task_add names {key}"
            if _int(a.get("continues")) in owners:
                return f"task_add continues #{a['continues']}"
        elif t == "task_update" and _int(a.get("id")) in owners:
            if changed := _changed(db, a, before):
                return f"task_update #{a['id']} changes {', '.join(changed)}"
        elif t == "obligation" and str(a.get("key") or "").strip() == key:
            new = {x for x in (_int(v) for v in a.get("owners") or []) if x is not None
                   and (x not in owners or since is not None
                        and float(((o.get("owners") or {}).get(str(x)) or {}).get("since") or 0) >= since)}
            if new:
                return f"new owner #{', #'.join(map(str, sorted(new)))}"
            why = " ".join(str(a.get("justify") or "").split())
            if len(why.split()) >= JUSTIFY_MIN_WORDS:
                return f"justify: {why[:160]}"
        elif t == "config_set" and str(a.get("key") or "").strip():
            ck = str(a["key"]).strip()
            neck = _bottleneck_text(db, o, owners) if neck is None else neck
            last = ck.rsplit(".", 1)[-1]
            if _names(neck, ck) or (len(last) >= 4 and _names(neck, last)):
                return f"config_set {ck} (its bottleneck)"
    return None


def batch_plans(db, actions: list, skip=(), now: float | None = None, since: float | None = None,
                before: dict[int, dict] | None = None) -> dict[str, str]:
    """{obligation id: plan change} for each open, missed promise this batch re-plans."""
    from . import obligations
    now = time.time() if now is None else now
    out = {}
    for o in obligations.items(db):
        pr = o.get("promise")
        if pr and obligations._open(o) and (missed(pr, now) or pr.get("misses")):
            if plan := plan_change(db, o, actions, skip, since, before):
                out[o["id"]] = plan
    return out


def flag_reply(p, st: dict, chat: str, mid: int, due: float | None, owners, text: str, plans: dict,
               now: float, skip: str | None = None) -> list[str]:
    """A reply in `chat` giving a new time (`due`) while a promise of that chat is past its due and the
    batch has no plan change for it: record the move and flag it. `skip`: the promise this reply updates
    (recorded by the caller). Returns notes."""
    from . import obligations
    if due is None:
        return []
    notes = []
    for o in st["items"].values():
        pr = o.get("promise")
        if not pr or o["id"] == skip or not obligations._open(o) or pr.get("chat") != chat or not missed(pr, now):
            continue
        mine = obligations._owner_ids(o)
        if mine and owners and not set(mine) & set(owners) and not _names(text, o["id"]) \
                and not any(_names(text, f"#{t}") for t in mine):
            continue   # a different deliverable in the same chat
        note_move(pr, due, now, plans.get(o["id"]), None, f"reply #{mid}")
        if not plans.get(o["id"]):
            notes += flag(p, o, mid, now)
    return notes


def flag(p, o: dict, mid: int, now: float) -> list[str]:
    """The flag on a reply that gave missed promise `o` a new time with no plan change."""
    from . import obligations
    fp = f"obligation:{o['id']}:moved:{mid}"
    text = (f"Message #{mid} gave {o['id']} a new time after it missed its due, and this batch changed nothing "
            f"on its plan. Say in that chat what changed, or re-plan it now. " + refusal(o, "earlier"))
    if obligations._raise(p.db, now, fp, FLAG_KIND, text, (obligations._owner_ids(o) or [None])[0]):
        return [f"{o['id']}: new time without a plan change flagged (message #{mid})"]
    return []


def outcomes(items: list[dict], since: float, now: float) -> dict:
    """The missed promises with a miss since `since`: how many, missed twice or more, due moves after a
    miss with no plan change and no user's message, and the median lateness in hours."""
    hit = [o for o in items if o.get("promise") and any(float(m["at"]) >= since for m in o["promise"].get("misses") or [])]
    late = []
    for o in hit:
        first = min(float(m["due"]) for m in o["promise"]["misses"])
        end = (o.get("receipt") or {}).get("at") or (o.get("dropped") or {}).get("at") or now
        late.append(max(0.0, float(end) - first) / 3600)
    bare = sum(1 for o in items if o.get("promise") for m in o["promise"].get("moves") or []
               if float(m["at"]) >= since and m.get("missed") and not m.get("plan") and not m.get("user"))
    return {"missed": len(hit), "twice": sum(len(o["promise"]["misses"]) >= 2 for o in hit),
            "bare_moves": bare, "median_late_h": round(statistics.median(late), 1) if late else None}


def audit_line(db, since: float, now: float | None = None) -> str:
    """The daily review's `missed promises` line; '' with none missed and no bare move."""
    from . import obligations
    now = time.time() if now is None else now
    r = outcomes(obligations.items(db), since, now)
    if not r["missed"] and not r["bare_moves"]:
        return ""
    return (f"missed promises, {(now - since) / 3600:g} h: {r['missed']} missed, {r['twice']} missed twice or more, "
            f"{r['bare_moves']} due moves without a plan change (target 0)"
            + (f", median lateness {r['median_late_h']:.1f} h" if r["median_late_h"] is not None else ""))

