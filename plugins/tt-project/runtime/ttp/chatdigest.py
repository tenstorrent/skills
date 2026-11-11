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

Status pace: a status post (progress, counts, ETAs; never an answer to the user's question, an ask, an outage
or a result: is_status) goes to a chat at most once per `coordinator.chat_status_gap_h` hours (default 1;
`coordinator.chat_status_gap_chats` {chat: hours} per chat); a sooner one is held for the digest. The daemon's
hourly table keeps the same gap. A post that nearly repeats what any other chat of the same user got in the
last hour (twin: most words alike, or the same task and obligation ids) is not sent again; the coordinator
records the earlier message as delivering that chat's promise on the topic (obligations.deliver_twins).
`coordinator.chat_users` {chat: user} tells apart chats of different users; unset, a project's chats are one
user's. `coordinator.chat_dedupe` false turns the twin check off."""
from __future__ import annotations

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


def _plain(db, text: str, a: dict, chat: str | None) -> str:
    """The post's body when it may be paced or deduplicated; '' for an outage, a high or critical post, an
    ask to the user or an answer to the user's open question, which always go."""
    body = str(text or "").split(FOOTER, 1)[0].strip()
    if (a.get("outage") is True or str(a.get("severity") or "").lower() in ("high", "critical")
            or a.get("type") == "ask_user" or asks_user(body) or _OUTAGE.search(body) or _asked(db, chat)):
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
    if not body or _MATTERS.search(body) or asks_user(body) or _asked(db, chat):
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


def hold(db, chat: str | None, text: str, why: str, now: float | None = None) -> None:
    """Keep `text` for `chat`'s next digest ('all': every chat)."""
    now = time.time() if now is None else now
    chat = chat or "all"
    with db.tx():
        st = db.kv(KV) or {}
        st.setdefault("held", {}).setdefault(chat, []).append({"ts": now, "text": str(text), "why": why})
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
                         f"decisions, outages and asks")
            return None
        if is_status(db, text, a, chat) and status_gap_s(cfg, chat):
            if (wait := status_wait(db, cfg, chat, now)) > 0:
                hold(db, chat, text, "status gap", now)
                notes.append(f"chat digest: your {kind} to chat {chat or 'all'} is a status, and that chat had one "
                             f"under {status_gap_s(cfg, chat) / 3600:g} h ago (coordinator.chat_status_gap_h), so "
                             f"it waits for the chat's next digest; the next status may go in {wait / 60:.0f} min")
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
            where = timefmt.home(p)
            lines = [f"- {timefmt.short(float(i['ts']), where, now)}: {' '.join(str(i['text']).split())}"
                     for i in items]
            text = (f"Digest: {len(items)} routine update{'s' * (len(items) != 1)} since "
                    f"{timefmt.short(float(items[0]['ts']), where, now)}\n" + "\n".join(lines))
            out += msgparts.post(db, text, chat=None if chat == "all" else chat, kind="info", severity="normal")
            held[chat], last[chat] = [], now
            _count(st, chat, "digests")
        db.set_kv(KV, st)
    return out


def counts(db) -> dict:
    """{chat: {"sent", "held", "digests", "deduped", "waiting"}}: posts sent at once, held, digests posted,
    near-repeats not sent, held now."""
    st = db.kv(KV) or {}
    chats = set(st.get("counts") or {}) | set(st.get("held") or {})
    return {c: {**{f: int((st.get("counts") or {}).get(c, {}).get(f) or 0) for f in ("sent", "held", "digests", "deduped")},
                "waiting": len((st.get("held") or {}).get(c) or [])} for c in sorted(chats)}
