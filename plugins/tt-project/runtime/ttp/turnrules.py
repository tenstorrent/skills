"""Model-free rules on a coordinator turn's actions (both coordinator modes apply turns through
coordinator.apply), with no free-text classifier over what the user or the coordinator wrote:

- Order or reason: a turn handling user message m records m's order (`obligation` with `order` m, or
  with `key` and `order` m to link an open obligation) or says why it carries none (`no_order` with a
  reason on a reply or noop; with `order` m it covers m alone, without it every message of the turn).
  A reply naming its message by `order` alone answers it (`no_order` ANSWERED: normal_form, _answers), and
  a message whose chat exactly one task_add of the batch replies to gets its order recorded, owned by
  that task (task_orders, from coordinator.apply). A turn with neither is refused once per message
  (order_gap): the daemon shows the message again to the next turn (daemon._keep_messages), and a second
  miss is let through. A reply the chat digest held answers nothing: its message keeps that refusal.
- Run results said once: a reply citing a run's result names the run ids in `cites`. A run already
  cited by an earlier reply to the same chat is refused unless the reply sets `repeat` true
  (check_cites, record_cites). A reply that cites nothing is not checked: nothing reads its words.
"""
from __future__ import annotations
from .util import as_int

NO_ORDER_REFUSED_KEY = "turnrules_no_order_refused"   # user message ids refused once for no order
NO_ORDER_KEPT = 200
NO_ORDER_ACTIONS = ("reply", "noop")
PREFIX = "order: "   # start of an order_gap refusal (coordinator.fixable_rejection)
ANSWERED = "answered in reply"   # the `no_order` a reply naming its message by `order` implies
CITES_KEY = "turnrules_cites"   # {chat: {run id: reply message id}}
CITES_KEPT = 500                # runs remembered per chat


def order_gap(db, covered: set[int], actions: list, failed: set[int], messages, held=()) -> str | None:
    """The refusal for the user messages of this turn (`messages`, a relayer's findings aside) with no
    order on the ledger (`covered`: obligations.order_messages after the turn) and no `no_order` on an
    applied reply or noop; None when each has one, or each was refused once already. A reply the chat
    digest held (`held`: action indexes) never reached the chat, so its `no_order` answers nothing."""
    ids = [m for m in (as_int(x) for x in messages or []) if m is not None]
    if not ids:
        return None
    users = [r["id"] for r in db.q(f"SELECT id FROM messages WHERE id IN ({','.join('?' * len(ids))}) "
                                   "AND direction='in' AND kind!='finding' ORDER BY id", ids)]
    held = set(held or ())
    if (covered := _covered(db, covered, actions, failed, held)) is None:
        return None
    refused = [m for m in (as_int(x) for x in db.kv(NO_ORDER_REFUSED_KEY, []) or []) if m is not None]
    new = [m for m in users if m not in covered and m not in refused]
    if not new:
        return None
    db.set_kv(NO_ORDER_REFUSED_KEY, (refused + new)[-NO_ORDER_KEPT:])
    shown = ", ".join(f"#{m}" for m in new)
    kept = sorted({m for i in held if i < len(actions or []) and isinstance(actions[i], dict)
                   and (m := as_int(actions[i].get("order"))) in new})
    if kept:   # the reply naming it was held for the digest: the user has not got it
        return (f"{PREFIX}user message {', '.join(f'#{m}' for m in kept)}: your reply answering it was held for "
                f"the chat digest and NOT delivered to the chat, so it answers nothing yet: send what they asked "
                f"for as a reply with `order` naming their message (a requested result goes at once), or record "
                f"it as an `obligation` with `order`" + (f"; also {', '.join(f'#{m}' for m in new if m not in kept)}"
                                                         f" has no order and no `no_order`" if len(kept) < len(new)
                                                         else ""))
    return (f"{PREFIX}user message {shown} has no order and no `no_order`: record it (`obligation` with `order` "
            f"(its id), `text`, `owners`, `action_class`; `key` and `order` link an open obligation), or put "
            f"`no_order` with the reason (a question answered, thanks, a status read) on a reply or noop, "
            f"with `order` naming the message")


def _chat(chat) -> str:
    return "all" if chat in (None, "", "all") else str(chat)


def _covered(db, covered, actions: list, failed, held: set) -> set[int] | None:
    """`covered` plus each message an applied reply or noop the digest did not hold gives a `no_order`, or
    answers by `order` alone (_answers: the daemon checks the batch as sent, normal_form rewrote apply's
    copy); None when a `no_order` without `order` covers every message of the turn."""
    covered = set(covered)
    for i, a in enumerate(actions or []):
        if not isinstance(a, dict) or i in failed or i in held or a.get("type") not in NO_ORDER_ACTIONS:
            continue
        if (m := _answers(db, a, actions)) is not None:
            covered.add(m)
        elif " ".join(str(a.get("no_order") or "").split()):
            if a.get("order") in (None, ""):
                return None
            if (m := as_int(a.get("order"))) is not None:
                covered.add(m)
    return covered


def _answers(db, a: dict, batch: list) -> int | None:
    """The user message reply `a` answers by `order` with no `no_order`: its `chat` is that message's (a reply
    naming none goes to the turn's default chat: two readings), and no obligation of `batch` records that
    message's order."""
    m = as_int(a.get("order"))
    if a.get("type") != "reply" or m is None or " ".join(str(a.get("no_order") or "").split()) \
            or any(isinstance(b, dict) and b.get("type") == "obligation" and as_int(b.get("order")) == m
                   for b in batch or []):
        return None
    row = db.one("SELECT chat FROM messages WHERE id=? AND direction='in' AND kind!='finding'", (m,))
    return m if row and a.get("chat") not in (None, "") and _chat(a["chat"]) == _chat(row["chat"]) else None


def normal_form(p, a: dict, batch: list, _home: str, _now: float):
    """actionforms: a reply answering a user message by `order` alone (_answers) carries `no_order` ANSWERED."""
    if (m := _answers(p.db, a, batch)) is None:
        return None
    return {**a, "no_order": ANSWERED}, f"no_order={ANSWERED!r} (was: none; the reply names user message #{m} " \
                                        f"by `order`)"


def task_orders(p, messages, actions: list, tasks: dict, failed, held=()) -> list[str]:
    """coordinator.apply, after the batch: a user message of the turn (`messages`) with no order and no
    `no_order` (as order_gap reads them), alone in its chat, whose chat exactly one task_add of the batch
    replies to (`reply_chat`), gets its order recorded, owned by the task that task_add added (`tasks`: action
    index -> task id), with the task's title as its text: a read order for a question, plan or review task,
    else a change order (obligations.add_order). One a held reply names keeps order_gap's held refusal.
    Returns an 'applied as' note per order."""
    from . import obligations, rejtally
    ids = [m for m in (as_int(x) for x in messages or []) if m is not None]
    held = set(held or ())
    if not ids or (covered := _covered(p.db, obligations.order_messages(p.db), actions, failed, held)) is None:
        return []
    covered |= {as_int(actions[i].get("order")) for i in held if i < len(actions) and isinstance(actions[i], dict)}
    rows = [r for r in p.db.q(f"SELECT id, chat FROM messages WHERE id IN ({','.join('?' * len(ids))}) "
                              "AND direction='in' AND kind!='finding' ORDER BY id", ids) if r["id"] not in covered]
    notes = []
    for r in rows:
        adds = [i for i, a in enumerate(actions) if isinstance(a, dict) and a.get("type") == "task_add"
                and a.get("reply_chat") and _chat(a["reply_chat"]) == _chat(r["chat"])]
        if len(adds) != 1 or adds[0] not in tasks or sum(_chat(x["chat"]) == _chat(r["chat"]) for x in rows) != 1:
            continue
        t = p.db.task(tasks[adds[0]])
        cls = "read" if t["kind"] in obligations.UNFIT_OWNER_KINDS else "change"
        done = obligations.add_order(p, t["title"], r["id"], [t["id"]], action_class=cls)
        notes.append(f"applied as: obligation order={r['id']} owners=[{t['id']}] action_class={cls!r} (was: no order "
                     f"and no `no_order`; task_add actions[{adds[0]}] #{t['id']} replies to its chat): {done}")
    rejtally.record(p.db, [], [], normalised=notes)
    return notes


def check_cites(db, a: dict, chat, notes: list | None = None) -> list[int]:
    """A reply's `cites` as run ids, before it is sent: each a known run, and none cited by an earlier
    reply to `chat` unless `repeat` is true. Raises ValueError naming the refused runs. With `notes`, a
    reply repeating a run goes out with only its new part (_new_part), the run dropped from its cites."""
    raw = a.get("cites")
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or any(as_int(x) is None or isinstance(x, bool) for x in raw):
        raise ValueError("`cites` is a list of run ids (the runs whose results the reply reports)")
    runs = sorted({int(x) for x in raw})
    known = {r["id"] for r in db.q(f"SELECT id FROM runs WHERE id IN ({','.join('?' * len(runs))})", runs)}
    if missing := [r for r in runs if r not in known]:
        raise ValueError(f"`cites`: no run {', '.join(f'#{r}' for r in missing)}")
    if a.get("repeat") is True:
        return runs
    seen = (db.kv(CITES_KEY, {}) or {}).get(_chat(chat)) or {}
    if (again := [r for r in runs if str(r) in seen]) and notes is not None \
            and (rest := _new_part(db, a["text"], {r: seen[str(r)] for r in again})):
        a["text"] = rest
        notes.append("reply: sent only its new part: the sentences " + ", ".join(
            f"repeating message #{seen[str(r)]} (run {r})" for r in again) + " were dropped, and the run(s) "
            "from its cites (already in this chat)")
        return [r for r in runs if r not in again]
    if again:
        raise ValueError("reply not sent: " + ", ".join(f"run {r}'s result went to this chat in message #{seen[str(r)]}"
                                                        for r in again)
                         + "; say only what is new, or set `repeat` true when the user asked for it again")
    return runs


def _new_part(db, text: str, again: dict[int, int]) -> str:
    """`text` without its sentences that repeat a sentence of the message that already reported a run of
    `again` (run id -> that message) word for word; '' (refused as sent) when none is dropped, nothing is
    left, or a sentence left still names such a run as 'run N' (it may carry new facts about it)."""
    import re
    said = set()
    for m in set(again.values()):
        row = db.one("SELECT text FROM messages WHERE id=?", (m,))
        said |= {" ".join(x.split()).lower() for x in re.split(r"(?<=[.!?])\s+|\n+", (row or {"text": ""})["text"] or "")}
    named = re.compile(r"\brun #?(?:" + "|".join(str(r) for r in again) + r")\b", re.I)
    parts = re.split(r"((?<=[.!?])\s+|\n+)", text or "")
    keep, dropped = [], False
    for i in range(0, len(parts), 2):
        s = parts[i]
        if s.strip() and " ".join(s.split()).lower() in said:
            dropped = True
            continue
        keep += [s, parts[i + 1] if i + 1 < len(parts) else ""]
    rest = "".join(keep).strip()
    return rest if dropped and re.search(r"\w", rest) and not named.search(rest) else ""


def record_cites(db, chat, runs: list[int], message: int) -> None:
    """Remember that reply `message` to `chat` reported `runs` (check_cites reads it)."""
    if not runs:
        return
    st = db.kv(CITES_KEY, {}) or {}
    seen = st.setdefault(_chat(chat), {})
    for r in runs:
        seen.pop(str(r), None)
        seen[str(r)] = int(message)
    st[_chat(chat)] = dict(list(seen.items())[-CITES_KEPT:])
    db.set_kv(CITES_KEY, st)
