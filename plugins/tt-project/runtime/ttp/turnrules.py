"""Model-free rules on a coordinator turn's actions (both coordinator modes apply turns through
coordinator.apply), with no free-text classifier over what the user or the coordinator wrote:

- Order or reason: a turn handling user message m records m's order (`obligation` with `order` m, or
  with `key` and `order` m to link an open obligation) or says why it carries none (`no_order` with a
  reason on a reply or noop; with `order` m it covers m alone, without it every message of the turn).
  A turn with neither is refused once per message (order_gap): the daemon shows the message again to
  the next turn (daemon._keep_messages), and a second miss is let through.
- Run results said once: a reply citing a run's result names the run ids in `cites`. A run already
  cited by an earlier reply to the same chat is refused unless the reply sets `repeat` true
  (check_cites, record_cites). A reply that cites nothing is not checked: nothing reads its words.
"""
from __future__ import annotations

NO_ORDER_REFUSED_KEY = "turnrules_no_order_refused"   # user message ids refused once for no order
NO_ORDER_KEPT = 200
NO_ORDER_ACTIONS = ("reply", "noop")
PREFIX = "order: "   # start of an order_gap refusal (coordinator.fixable_rejection)
CITES_KEY = "turnrules_cites"   # {chat: {run id: reply message id}}
CITES_KEPT = 500                # runs remembered per chat


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def order_gap(db, covered: set[int], actions: list, failed: set[int], messages) -> str | None:
    """The refusal for the user messages of this turn (`messages`, a relayer's findings aside) with no
    order on the ledger (`covered`: obligations.order_messages after the turn) and no `no_order` on an
    applied reply or noop; None when each has one, or each was refused once already."""
    ids = [m for m in (_int(x) for x in messages or []) if m is not None]
    if not ids:
        return None
    users = [r["id"] for r in db.q(f"SELECT id FROM messages WHERE id IN ({','.join('?' * len(ids))}) "
                                   "AND direction='in' AND kind!='finding' ORDER BY id", ids)]
    covered = set(covered)
    for i, a in enumerate(actions or []):
        if not isinstance(a, dict) or i in failed or a.get("type") not in NO_ORDER_ACTIONS \
                or not " ".join(str(a.get("no_order") or "").split()):
            continue
        if a.get("order") in (None, ""):
            return None
        if (m := _int(a.get("order"))) is not None:
            covered.add(m)
    refused = [m for m in (_int(x) for x in db.kv(NO_ORDER_REFUSED_KEY, []) or []) if m is not None]
    new = [m for m in users if m not in covered and m not in refused]
    if not new:
        return None
    db.set_kv(NO_ORDER_REFUSED_KEY, (refused + new)[-NO_ORDER_KEPT:])
    shown = ", ".join(f"#{m}" for m in new)
    return (f"{PREFIX}user message {shown} has no order and no `no_order`: record it (`obligation` with `order` "
            f"(its id), `text`, `owners`, `action_class`; `key` and `order` link an open obligation), or put "
            f"`no_order` with the reason (a question answered, thanks, a status read) on a reply or noop, "
            f"with `order` naming the message")


def _chat(chat) -> str:
    return "all" if chat in (None, "", "all") else str(chat)


def check_cites(db, a: dict, chat) -> list[int]:
    """A reply's `cites` as run ids, before it is sent: each a known run, and none cited by an earlier
    reply to `chat` unless `repeat` is true. Raises ValueError naming the refused runs."""
    raw = a.get("cites")
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or any(_int(x) is None or isinstance(x, bool) for x in raw):
        raise ValueError("`cites` is a list of run ids (the runs whose results the reply reports)")
    runs = sorted({int(x) for x in raw})
    known = {r["id"] for r in db.q(f"SELECT id FROM runs WHERE id IN ({','.join('?' * len(runs))})", runs)}
    if missing := [r for r in runs if r not in known]:
        raise ValueError(f"`cites`: no run {', '.join(f'#{r}' for r in missing)}")
    if a.get("repeat") is True:
        return runs
    seen = (db.kv(CITES_KEY, {}) or {}).get(_chat(chat)) or {}
    if again := [r for r in runs if str(r) in seen]:
        raise ValueError("reply not sent: " + ", ".join(f"run {r}'s result went to this chat in message #{seen[str(r)]}"
                                                        for r in again)
                         + "; say only what is new, or set `repeat` true when the user asked for it again")
    return runs


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
