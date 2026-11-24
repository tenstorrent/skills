"""Chat only for what matters: routine coordinator posts wait in one short digest per chat.

A coordinator reply or notify reaches its chat at once only when it matters: a finished result
(`delivers`, `cites`, words naming one), a decision the user may want to override, an outage, an ask or
question to the user, an answer to the user's question, a promise or claim (a time it commits to, `claims`),
a step it asks the user to take (a command for them, a sign-in or approval), or a high or critical severity. A post that is
only an acknowledgement ('Got it', 'On it'), only progress or queue waits ('queued', 'waits on', 'ETA'),
or only a correction of a forecast is held (why_hold) and never dropped: hold() keeps it on disk, and
flush() posts every held item of a chat as one digest once the oldest has waited
`coordinator.chat_digest_h` hours (default 3) and the chat's last digest is that old too. The status
footer onhost.guard appends ('Status now: ...') leaves a post that is sent and goes in the digest; the
sentence it marked as planned stays. The daemon's own model-free must-act update goes in the digest too,
unless urgent(): an outage or a blocker only the user can clear posts at once.
Held text is kept whole, so a reminder of a pending user action keeps its full command and options.
`coordinator.chat_digest_h` 0 sends every post at once. counts() feeds `ttp stats --chat`.
The digest itself, like the hourly status table, stays in the web app (recorded with no chat, on the
quiet channel).

Status pace: a status post (progress, counts, ETAs; never an answer to the user's question, an ask, an outage
or a result: is_status) goes to a chat at most once per `coordinator.chat_status_gap_h` hours (default 1;
`coordinator.chat_status_gap_chats` {chat: hours} per chat); a sooner one is held for the digest. A reply
whose `order` names the user's request for that content ('send an ETA and the plan', requested) is a
requested result, never status: it goes at once. A held post never answers its order (it is not a sent
message), and only a post that reached the chat starts its status gap; the
coordinator's note on a held post says the chat never got it, naming its order. A post that nearly repeats what any other chat of the same user got in the
last hour (twin: most words alike, or the same task and obligation ids) is not sent again; the earlier
message delivers only what the reply's own `delivers` named (obligations.deliver_twins), never by topic.
`coordinator.chat_users` {chat: user} tells apart chats of different users; unset, a project's chats are one
user's. `coordinator.chat_dedupe` false turns the twin check off.

Daemon hand-offs (handoff): a finished task's hand-off never posts to its chat. The daemon raises one
REPLY_DUE event per finished reply_chat task, naming its chat and its landing facts with the hand-off
enclosed as data, and the coordinator replies to the chat in plain words; the task's promises still record
in its chat (obligations.record_promises, by_task this task). A fallback outcome line (outcome_line) posts
model-free, from records only, when no reply reaches that chat within coordinator.reply_fallback_s.
Every daemon path that posts kind info or reply to a chat goes through status_wait, hold or checked (a guard
test reads the posting code paths)."""
from __future__ import annotations

import json
import os
import re
import time

KV = "chat_digest"   # {"held": {chat: [{ts, text, why}]}, "last": {chat: ts}, "status": {chat: ts},
#                       "counts": {chat: {sent, held, digests, deduped}}}
DEFAULT_H = 3
STATUS_GAP_H = 1
TWIN_S = 3600   # a near-repeat within this long of a post to another chat of the same user is not sent
FOOTER = "\nStatus now: "   # onhost.guard's appended status line

_MATTERS = re.compile(
    r"\?|\b(?:done|finished|completed?|landed|pushed|merged|passed|fail(?:ed|s|ure)?|broken|fixed|results?|"
    r"outage|down|unreachable|offline|decid(?:e|ed|ing)|decision|chose|picked|override|blocked|approve|"
    r"need (?:you|your)|your (?:yes|word|answer|call|go)|ask \d+|ready)\b", re.I)
_ACK = re.compile(r"^\W*(?:got it|on it|noted|ok(?:ay)?|will do|understood|acknowledged|thanks|thank you|"
                  r"sure|roger|yes|right)\b", re.I)
_PROGRESS = re.compile(
    r"\b(?:queued|waits?|waiting|still (?:running|queued|waiting)|in progress|under way|eta|expected finish|"
    r"on track|no change|unchanged|held (?:by|for|behind)|behind)\b", re.I)
_CORRECTION = re.compile(
    r"\b(?:correction|corrected|earlier (?:i|we) said|(?:new|revised|updated) (?:eta|time|forecast)|"
    r"eta (?:is )?now|now (?:due|expected))\b", re.I)

# A step the user is asked to take: sign-in, approval, a credential, 'reply here'.
_USER_STEP = re.compile(
    r"\b(?:approv\w*|sign(?:ed)?[- ]?in|log(?:ged)?[- ]?in|login|credentials?|passwords?|reply (?:here|to this)|"
    r"only you|only the user|your (?:sign|approval|login|step))\b", re.I)
_YOU = re.compile(r"\b(?:you|your)\b", re.I)
_OUTAGE = re.compile(r"\b(?:outage|down|unreachable|offline|crash(?:ed|es)?|dead|not responding)\b", re.I)
_STATUS = re.compile(r"\b(?:status|backlog|queue[ds]?|eta|running|remaining|\d+\s+(?:tasks?|runs?|items?|left|open|"
                     r"queued|running|waiting|done))\b", re.I)
_NOT_STATUS = re.compile(r"\?|\b(?:decid\w*|decision|chose|picked|override|fail(?:ed|s|ure)?|broken|"
                         r"need (?:you|your)|your (?:yes|word|answer|call|go)|ask \d+)\b", re.I)
# A user message that asks for content: a question, or a request for an ETA, a plan, a number, a table, a result.
_REQUEST = re.compile(r"\?|\b(?:send|give|tell|show|post|report|share|list|explain|summari[sz]e|what|when|where|"
                      r"which|why|how|eta|plan|table|numbers?|figures?|results?|status|estimate)\b", re.I)
REQUEST_S = 24 * 3600   # a reply's `order` names a request made at most this long ago
ANSWER_S = 600   # parts of one requested answer: replies within this long of the first one sent after the request


def asks_user(text: str) -> bool:
    """`text` asks the user to act: a backticked command addressed to them, or sign-in, approval or
    credential wording. Such a post is an ask, never routine."""
    body = str(text or "")
    return bool(_USER_STEP.search(body) or ("`" in body and re.search(r"`[^`\n]+`", body) and _YOU.search(body)))


def urgent(text: str) -> bool:
    """A model-free update that must reach the chat at once: an outage, or a blocker only the user can clear."""
    return bool(_OUTAGE.search(str(text or "")) or asks_user(text))


def every_s(cfg: dict | None, chat: str | None = None) -> float:
    """Seconds between a chat's digests; 0: no digest, every post goes at once. A chat can set its own
    hours in `coordinator.chat_digest_chats` ({chat: hours}); the broadcast chat is 'all'."""
    if os.environ.get("TTP_TEST_CHAT_DIGEST") == "off":   # the test suite's fixture: other tests see posts unchanged
        return 0.0
    co = ((cfg or {}).get("coordinator") or {})
    own = co.get("chat_digest_chats")
    try:
        if isinstance(own, dict) and str(chat or "all") in own:
            return max(0.0, float(own[str(chat or "all")])) * 3600
        return max(0.0, float(co.get("chat_digest_h", DEFAULT_H))) * 3600
    except (TypeError, ValueError):
        return DEFAULT_H * 3600.0


def status_gap_s(cfg: dict | None, chat: str | None = None) -> float:
    """Seconds a chat waits between status posts; 0 (also with its digest off): no gap."""
    if every_s(cfg, chat) <= 0:
        return 0.0
    co = ((cfg or {}).get("coordinator") or {})
    own = co.get("chat_status_gap_chats")
    try:
        if isinstance(own, dict) and str(chat or "all") in own:
            return max(0.0, float(own[str(chat or "all")])) * 3600
        return max(0.0, float(co.get("chat_status_gap_h", STATUS_GAP_H))) * 3600
    except (TypeError, ValueError):
        return STATUS_GAP_H * 3600.0


def requested(db, a: dict, chat: str | None, now: float | None = None) -> int | None:
    """The user message a reply answers when it was asked for: its `order` names an inbound user message
    (a relayer's findings aside) in this chat, at most REQUEST_S old, that asks for content (_REQUEST: a
    question, an ETA, a plan, a number, a table, a result), and no reply went to the chat after it, or
    the first one did under ANSWER_S ago (a multi-part answer). None otherwise. A held post is no reply:
    it is not in the messages table, so it never counts as having answered the order."""
    try:
        m = int(a.get("order"))
    except (TypeError, ValueError):
        return None
    if a.get("type") not in (None, "reply"):
        return None
    now = time.time() if now is None else now
    row = db.one("SELECT id, ts, chat, direction, kind, text FROM messages WHERE id=?", (m,))
    if not row or row["direction"] != "in" or str(row["kind"] or "") == "finding" \
            or now - float(row["ts"] or 0) > REQUEST_S or not _REQUEST.search(str(row["text"] or "")):
        return None
    if chat and chat != "all" and row["chat"] not in (None, "", chat):
        return None
    first = db.one("SELECT ts FROM messages WHERE direction='out' AND kind='reply' AND chat=? AND id>? "
                   "ORDER BY id LIMIT 1", (row["chat"], m)) if row["chat"] else None
    if first and now - float(first["ts"] or 0) > ANSWER_S:
        return None
    return m


def _plain(db, text: str, a: dict, chat: str | None) -> str:
    """The post's body when it may be paced or deduplicated; '' for an outage, a high or critical post, an
    ask to the user, an answer to the user's open question or a reply the user asked for (requested),
    which always go."""
    body = str(text or "").split(FOOTER, 1)[0].strip()
    if (a.get("outage") is True or str(a.get("severity") or "").lower() in ("high", "critical")
            or a.get("type") == "ask_user" or asks_user(body) or _OUTAGE.search(body) or _asked(db, chat)
            or requested(db, a, chat) is not None):
        return ""
    return body


def is_status(db, text: str, a: dict, chat: str | None) -> bool:
    """A status post: progress, counts or ETAs, not a result (`delivers`, `cites`), a decision, a failure,
    a question, an ask or an outage."""
    body = "" if a.get("delivers") or a.get("cites") else _plain(db, text, a, chat)
    return bool(body and not _NOT_STATUS.search(body) and (_STATUS.search(body) or _PROGRESS.search(body)))


def status_wait(db, cfg: dict | None, chat: str | None, now: float | None = None) -> float:
    """Seconds until `chat` may get its next status post (0: now)."""
    gap = status_gap_s(cfg, chat)
    last = float(((db.kv(KV) or {}).get("status") or {}).get(chat or "all") or 0)
    return max(0.0, gap - ((time.time() if now is None else now) - last)) if gap else 0.0


def mark_status(db, chat: str | None, now: float | None = None) -> None:
    """Record a status post to `chat` now: the next one waits status_gap_s."""
    with db.tx():
        st = db.kv(KV) or {}
        st.setdefault("status", {})[chat or "all"] = time.time() if now is None else now
        db.set_kv(KV, st)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(text or "").split(FOOTER, 1)[0].lower()))


def _topic(text: str) -> set[str]:
    return set(re.findall(r"#\d+|\bo\d+\b", str(text or "").lower()))


def near(a: str, b: str) -> bool:
    """Near-duplicate texts: 80% of their words alike, or the same task and obligation ids and half alike."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    j = len(wa & wb) / len(wa | wb)
    return j >= 0.8 or (j >= 0.5 and bool(_topic(a)) and _topic(a) == _topic(b))


def twin(db, cfg: dict | None, text: str, chat: str, now: float | None = None) -> int | None:
    """The id of a post of the last hour to another chat of the same user (or to every chat) that `text`
    nearly repeats; None if none."""
    now = time.time() if now is None else now
    users = ((cfg or {}).get("coordinator") or {}).get("chat_users")
    users = users if isinstance(users, dict) else {}
    for m in db.q("SELECT id, chat, text FROM messages WHERE direction='out' AND kind != 'ask' AND ts >= ? "
                  "AND (chat IS NULL OR chat != ?) ORDER BY id DESC LIMIT 100", (now - TWIN_S, chat)):
        if m["chat"] and users.get(m["chat"], "") != users.get(chat, ""):
            continue
        if near(text, m["text"]):
            return int(m["id"])
    return None


def _asked(db, chat: str | None) -> bool:
    """The chat's newest message is the user's, asks a question and has no reply yet."""
    if not chat or chat == "all":
        return False
    last = db.one("SELECT direction, text FROM messages WHERE chat=? ORDER BY id DESC LIMIT 1", (chat,))
    return bool(last and last["direction"] == "in" and "?" in str(last["text"] or ""))


def why_hold(db, text: str, a: dict, chat: str | None) -> str:
    """Why this post waits for the digest ('ack', 'progress', 'forecast correction'); '' sends it now."""
    if (a.get("delivers") or a.get("cites") or a.get("claims") or a.get("outage") is True
            or str(a.get("severity") or "").lower() in ("high", "critical")):
        return ""
    body = str(text or "").split(FOOTER, 1)[0].strip()
    if not body or _MATTERS.search(body) or asks_user(body) or _asked(db, chat) or requested(db, a, chat) is not None:
        return ""
    from . import obligations   # a promise the user relies on is not routine: it goes now and is tracked
    if obligations.commitments(body):
        return ""
    if _ACK.match(body) and len(body) <= 160:
        return "ack"
    if _CORRECTION.search(body):
        return "forecast correction"
    parts = [s for s in re.split(r"(?<=[.!;])\s+|\n", body) if s.strip()]
    if parts and all(_PROGRESS.search(s) for s in parts):
        return "progress"
    return ""


def _count(st: dict, chat: str, field: str) -> None:
    c = st.setdefault("counts", {}).setdefault(chat, {})
    c[field] = int(c.get(field) or 0) + 1


def hold(db, chat: str | None, text: str, why: str, now: float | None = None, task: int | None = None) -> None:
    """Keep `text` for `chat`'s next digest ('all': every chat); `task`: the task a hand-off line is of."""
    now = time.time() if now is None else now
    chat = chat or "all"
    with db.tx():
        st = db.kv(KV) or {}
        st.setdefault("held", {}).setdefault(chat, []).append(
            {"ts": now, "text": str(text), "why": why, **({"task": int(task)} if task is not None else {})})
        _count(st, chat, "held")
        db.set_kv(KV, st)


def routine(p, chat: str | None, text: str, why: str, now: float | None = None, at_once: bool = False) -> bool:
    """A model-free routine update: held for the chat's digest (True), or posted now with the digest off or
    `at_once` (an outage or a blocker only the user can clear)."""
    if not at_once and every_s(p.config(), chat) > 0:
        hold(p.db, chat, text, why, now)
        return True
    from . import msgparts
    msgparts.post(p.db, text, chat=None if chat in (None, "all") else chat, kind="info", severity="normal")
    return False


def _unseen(chat: str | None, a: dict) -> str:
    """The note's tail for a held post the chat will never get: the digest reaches only the web app,
    so a held post's order stays unanswered in the chat."""
    try:
        order = f" (order #{int(a.get('order'))} stays open)"
    except (TypeError, ValueError):
        order = ""
    return (f". It was NOT delivered to chat {chat or 'all'}: held posts reach only the web app, "
            f"so the user has not seen it{order}; if they asked for it, resend it "
            f"with `order` naming their message, or with `delivers` or `cites`")


def checked(db, cfg: dict | None, text: str, a: dict, chat: str | None, notes: list,
            now: float | None = None, twins: list | None = None) -> str | None:
    """The text to post now, or None when the whole post was held for the digest or nearly repeats a post
    to another chat of the user (its id and this chat go on `twins`). A post that goes keeps its body; an
    onhost status footer on it goes in the digest. Any fault sends the text unchanged."""
    try:
        kind = a.get("type") or "reply"
        now = time.time() if now is None else now
        if (chat and chat != "all" and os.environ.get("TTP_TEST_CHAT_DIGEST") != "off"
                and ((cfg or {}).get("coordinator") or {}).get("chat_dedupe", True) is not False
                and _plain(db, text, a, chat) and (first := twin(db, cfg, text, chat, now))):
            with db.tx():
                st = db.kv(KV) or {}
                _count(st, chat, "deduped")
                db.set_kv(KV, st)
            if twins is not None:
                twins.append((first, chat))
            notes.append(f"chat digest: your {kind} to chat {chat} was not sent: it nearly repeats message #{first}, "
                         f"posted to another chat of the same user in the last hour; that post stands for it")
            return None
        if every_s(cfg, chat) <= 0:
            return text
        why = why_hold(db, text, a, chat)
        if why:
            hold(db, chat, text, why, now)
            notes.append(f"chat digest: your {kind} to chat {chat or 'all'} was {why} only, so it waits for that "
                         f"chat's next digest (coordinator.chat_digest_h). Post to a chat only for results, "
                         f"decisions, outages and asks" + _unseen(chat, a))
            return None
        if is_status(db, text, a, chat) and status_gap_s(cfg, chat):
            if (wait := status_wait(db, cfg, chat, now)) > 0:
                hold(db, chat, text, "status gap", now)
                notes.append(f"chat digest: your {kind} to chat {chat or 'all'} is a status, and that chat had one "
                             f"under {status_gap_s(cfg, chat) / 3600:g} h ago (coordinator.chat_status_gap_h), so "
                             f"it waits for the chat's next digest; the next status may go in {wait / 60:.0f} min"
                             + _unseen(chat, a))
                return None
            mark_status(db, chat, now)
        body, sep, footer = str(text).partition(FOOTER)
        if sep:
            hold(db, chat, "Status now: " + footer.strip(), "status footer", now)
            text = body.rstrip()
        with db.tx():
            st = db.kv(KV) or {}
            _count(st, chat or "all", "sent")
            db.set_kv(KV, st)
    except Exception as e:   # a message is never lost to this filter
        notes.append(f"chat digest: the filter failed ({type(e).__name__}: {str(e)[:120]}); the post went as is")
    return text


def flush(p, cfg: dict | None, now: float | None = None) -> list[int]:
    """Post each chat's held items as one digest once they and its last digest are `chat_digest_h` old;
    with the digest off, at once. The message ids posted."""
    from . import msgparts, timefmt
    now = time.time() if now is None else now
    db = p.db
    out: list[int] = []
    with db.tx():
        st = db.kv(KV) or {}
        held, last = st.setdefault("held", {}), st.setdefault("last", {})
        for chat, items in list(held.items()):
            gap = every_s(cfg, chat)
            if not items or (gap and (now - min(float(i["ts"]) for i in items) < gap
                                      or now - float(last.get(chat) or 0) < gap)):
                continue
            if status_wait(db, cfg, None if chat == "all" else chat, now) > 0:   # never right after a status
                continue
            where = timefmt.home(p)
            lines = [f"- {timefmt.short(float(i['ts']), where, now)}: {' '.join(str(i['text']).split())}"
                     for i in items]
            text = (f"Digest: {len(items)} routine update{'s' * (len(items) != 1)} since "
                    f"{timefmt.short(float(items[0]['ts']), where, now)}\n" + "\n".join(lines))
            from . import alerts   # the web app only: no chat, the quiet channel
            where_to = "" if chat == "all" else f" (for chat {chat})"
            out += msgparts.post(db, text.replace("\n", where_to + "\n", 1), chat=None, channel=alerts.QUIET,
                                 kind="info", severity="normal")
            held[chat], last[chat] = [], now
            _count(st, chat, "digests")
        db.set_kv(KV, st)
    return out


REPLY_DUE = "reply_due"          # the event a finished reply_chat task raises for the coordinator
FALLBACK_KV = "reply_fallback"   # kv: the armed fallbacks [{chat, task, title, at, due, facts}]
DEFAULT_FALLBACK_S = 1200       # coordinator.reply_fallback_s: how long the coordinator has to reply
LINE_MAX = 300                  # a fallback outcome line stays under this
HANDOFF_CHARS = 3500            # the enclosed hand-off is cut at this many chars


def outcome_words(facts: dict) -> str:
    """The landing facts of a finished reply_chat task in words, for the reply_due event and the
    fallback line: how the task ended, what landed where, and whether it is deployed."""
    bits = [str(facts.get("status") or "done")]
    if facts.get("landed"):
        bit = ("landed as " + str(facts.get("sha") or "?")[:7]
               + f" (version {facts.get('version') or '?'}) on {facts.get('branch') or '?'}")
        if facts.get("deploy"):
            bit += ", " + str(facts["deploy"])
        bits.append(bit)
    else:
        bits.append("not landed")
    return ", ".join(bits)


def outcome_line(item: dict, facts: dict) -> str:
    """The fallback line for a chat the coordinator did not reply to: the task, whether it landed
    (commit and version), whether it is deployed, and any user step on record. No hand-off prose.
    `item` is the armed fallback: {chat, task, title, facts}."""
    head = f"#{item['task']} " + " ".join(str(item.get("title") or "").split())
    line = f"{head[:200]}: {outcome_words(facts)}"
    if (need := str(facts.get("need") or "").strip()):
        line += "\nNeeds from you: " + need
    return line if len(line) <= LINE_MAX else line[:LINE_MAX - 3].rstrip() + "..."


def _cfg(db):
    """The project's config for callers that hold only its db (pushq): the config lives one level up."""
    from pathlib import Path
    from .project import Project
    return Project(Path(db.path).parent.parent).config()


def fallback_s(cfg: dict | None) -> float:
    """`coordinator.reply_fallback_s`: how long the coordinator has to reply before the fallback line."""
    try:
        return max(0.0, float((((cfg or {}).get("coordinator") or {}).get("reply_fallback_s"))
                              or DEFAULT_FALLBACK_S))
    except (TypeError, ValueError):
        return DEFAULT_FALLBACK_S


def handoff(db, cfg: dict | None, task: dict, new: str, text: str, now: float | None = None, p=None,
            facts: dict | None = None) -> list[int]:
    """A finished reply_chat task's hand-off never posts to its chat; the user gets the coordinator's
    plain outcome summary instead. This raises one REPLY_DUE event that wakes the coordinator, naming
    the chat and the landing facts, with the hand-off enclosed as data only, and arms the model-free
    fallback line for the case no reply reaches the chat in time (fallbacks). The hand-off's promises
    still record in its chat. The message ids posted (always []: nothing posts here)."""
    from . import msgparts, spans
    chat = "all" if task.get("reply_chat") in (None, "", "all") else task["reply_chat"]
    now = time.time() if now is None else now
    f = {"status": new, **(facts or {})}
    tid = int(task["id"])
    body = msgparts.cut(text, "the hand-off")[:HANDOFF_CHARS]
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task,data) VALUES(?,?,?,?,?,?,?,?)",
         (now, "task:%d" % tid, REPLY_DUE, "normal",
          "#%d %s -> %s in chat %s: %s. Reply to that chat in plain words with the outcome (what is "
          "live, what the user must do), `delivers` naming #%d. Hand-off: %s" % (
              tid, str(task.get("title") or "")[:80], new, chat, outcome_words(f), tid,
              spans.wrap(body, "hand-off of #%d" % tid, spans.new_tag())),
          "queued", tid, json.dumps({"chat": chat, "task": tid, "facts": f})))
    st = db.kv(FALLBACK_KV) or []
    st.append({"chat": chat, "task": tid, "title": str(task.get("title") or ""),
               "at": now, "due": now + fallback_s(cfg or _cfg(db)), "facts": f})
    with db.tx():
        db.set_kv(FALLBACK_KV, st)
    if p is not None and new == "done":
        from . import obligations
        try:   # the promises its hand-off makes stay owed in that chat, closed by the reply that delivers
            obligations.record_promises(p, [(0, chat, body)], {}, now=now, by_task=tid)
        except Exception:
            pass
    return []


def fallbacks(p, now: float | None = None) -> list[int]:
    """Post the fallback outcome line (outcome_line) for each armed chat no reply reached within its
    bound (a message out to that chat since the hand-off). The message ids."""
    from . import msgparts
    now = time.time() if now is None else now
    st = p.db.kv(FALLBACK_KV) or []
    out: list[int] = []
    keep = []
    for item in st:
        if now < float(item.get("due") or 0):
            keep.append(item)
            continue
        if _replied(p.db, item):
            continue   # the coordinator's reply reached that chat: no fallback is needed
        out += msgparts.post(p.db, outcome_line(item, item.get("facts") or {}),
                             chat=None if item.get("chat") == "all" else item.get("chat"),
                             kind="reply", severity="normal")
    if len(keep) != len(st):
        p.db.set_kv(FALLBACK_KV, keep)
    return out


def _replied(db, item: dict) -> bool:
    """Whether a message reached the chat of `item` since its hand-off armed the fallback."""
    chat = item.get("chat")
    return bool(db.one("SELECT 1 FROM messages WHERE direction='out' AND ts>=? AND channel='chat' "
                       "AND (chat=? OR (chat IS NULL AND ?='all')) LIMIT 1",
                       (float(item.get("at") or 0), chat, chat)))


def counts(db) -> dict:
    """{chat: {"sent", "held", "digests", "deduped", "waiting"}}: posts sent at once, held, digests posted,
    near-repeats not sent, held now."""
    st = db.kv(KV) or {}
    chats = set(st.get("counts") or {}) | set(st.get("held") or {})
    return {c: {**{f: int((st.get("counts") or {}).get(c, {}).get(f) or 0) for f in ("sent", "held", "digests", "deduped")},
                "waiting": len((st.get("held") or {}).get(c) or [])} for c in sorted(chats)}
