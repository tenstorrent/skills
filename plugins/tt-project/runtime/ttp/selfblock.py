# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Self-blocking: work held for the user's word on a decision that is the coordinator's own.

Only five kinds of step wait for the user (GATES): (a) one that could affect people badly (destroying
someone's work or data, disturbing a machine or service others use, rewriting a branch others push
to), (b) one that speaks as the user (a post or message in their name, a PR leaving draft, a merge,
another person's request), (c) a credential, money or a step only the user holds (spend caps, a
sign-in, a physical step), (d) a charter Restriction, changed through its own machinery, (e) a pause
or hold the user set. Anything else (a test gate's scope, a hold's scope, pacing, landing order, a
project setting) is the coordinator's: decide, act, record.

Model-free, once a minute: a blocked or waiting task whose reason cites the user's word or waits
for the user (CITE), a coordinator reply from the last hour that holds work for the user (HOLD: a
bare 'your word' or 'your call' is ordinary in replies, so only hold wording counts), or an open ask whose
`blocking` is irreversible or restriction, none of whose text names one of GATES, raises one KIND
event naming the decide-and-record path. One event per check lists every new finding; each finding
(source, id and its text) raises once, across restarts (kv RAISED_KEY). Quiet for a task held by a
user-only setting (settinghold.py) or on an ask only the user can answer (review, merge, human,
access, funds, spend), and for a throughput-class observation mute whose owner is 'outside' (its
`why` names no GATES entry and cites no user message or ask; throughmute.vet). It never changes
a task or an ask: the turn it wakes decides. Replies raise at
most one event per REPLY_WINDOW_S, and a reply posted in that window never raises (kv REPLY_RAISED_KEY),
so the turn's own answer to the event cannot raise the next one.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

KIND = "self_blocking"
RAISED_KEY = "self_blocking_raised"   # kv: {fingerprint: ts raised}
REPLY_RAISED_KEY = "self_blocking_reply_raised"   # kv: ts of the last event listing a reply
KEEP_S = 8 * 86400
REPLY_WINDOW_S = 3600
MAX_LISTED = 8
USER_ONLY_ASKS = ("review", "merge", "human", "access", "funds", "spend")
CHECKED_ASKS = ("irreversible", "restriction")

_U = r"(?:the\s+user|you)"
_POSS = (r"(?:(?:the\s+)?user'?s|your)\s+(?:own\s+)?(?:word|ok|okay|approval|go-?ahead|say-?so|sign-?off|yes|"
         r"confirmation|permission|call|decision|answer|consent)")
# The reason rests on the user's word, or waits for the user.
CITE = re.compile(
    r"\b(?:the\s+)?user'?s\s+(?:own\s+)?(?:word|ok|okay|approval|go-?ahead|say-?so|sign-?off|yes|"
    r"confirmation|permission|call|decision|answer|consent)\b|"
    r"\byour\s+(?:own\s+)?(?:word|ok|okay|approval|go-?ahead|say-?so|sign-?off|yes|confirmation|permission|call|decision|consent)\b|"
    rf"\b(?:wait(?:s|ing|ed)?|held|hold(?:s|ing)?|pending|blocked|parked)\s+(?:for|on|until)\s+{_U}\b|"
    rf"\buntil\s+{_U}\s+(?:says?|confirms?|approves?|answers?|repl(?:y|ies)|decides?|agrees?|ok'?s)\b|"
    rf"\bonly\s+{_U}\s+(?:can|may|could)\s+(?:change|decide|approve|allow|set|lift|adjust|turn|widen|narrow|pick)\b|"
    rf"\b(?:needs?|requires?)\s+{_U}\s+to\s+(?:approve|confirm|decide|say|agree|sign\s+off)\b|"
    r"\b(?:pending|awaiting)\s+(?:a\s+)?user\s+(?:approval|decision|confirmation|answer)\b|"
    r"\buser\s+(?:approval|decision|confirmation)\s+(?:is\s+)?(?:needed|required)\b", re.I)
# A reply that holds work for the user: CITE without the bare possessive ("your call", "your word
# reverses it"), which is how a reply hands a decision over or records one.
HOLD = re.compile(
    rf"\b(?:wait(?:s|ing|ed)?|held|hold(?:s|ing)?|pending|blocked|parked)\s+(?:for|on|until)\s+(?:{_U}|{_POSS})\b|"
    rf"\buntil\s+(?:{_U}\s+(?:says?|confirms?|approves?|answers?|repl(?:y|ies)|decides?|agrees?|ok'?s)|{_POSS})\b|"
    rf"\bonly\s+(?:on|with|after|by)\s+{_POSS}\b|"
    rf"\bonly\s+{_U}\s+(?:can|may|could)\s+(?:change|decide|approve|allow|set|lift|adjust|turn|widen|narrow|pick)\b|"
    rf"\b(?:needs?|requires?)\s+{_U}\s+to\s+(?:approve|confirm|decide|say|agree|sign\s+off)\b|"
    r"\b(?:pending|awaiting)\s+(?:a\s+)?user\s+(?:approval|decision|confirmation|answer)\b|"
    r"\buser\s+(?:approval|decision|confirmation)\s+(?:is\s+)?(?:needed|required)\b", re.I)
# The five kinds of step that do wait for the user, each by its plain wording.
GATES = (
    ("affects people", re.compile(
        r"\b(?:delet\w*|destroy\w*|wip(?:e|es|ed|ing)|purg\w*|overwrit\w*|force[- ]?push\w*|"
        r"rewrit\w*\s+(?:the\s+|its\s+)?(?:history|branch)|someone\w*|other\s+(?:users?|people|teams?|tenants?|projects?)|"
        r"another\s+(?:user|person|team|tenant|project)|others\s+(?:use|share|push)|tenant\w*|"
        r"shared\s+(?:machine|box|node|host|cluster|service|branch|device|queue)|(?:in\s+use|used)\s+by)\b", re.I)),
    ("speaks as the user", re.compile(
        r"\b(?:merg\w*|ready\s+for\s+review|out\s+of\s+draft|leaves?\s+draft|undraft\w*|publish\w*|post(?:s|ed|ing)?|"
        r"e-?mail\w*|slack|send\s+(?:a\s+|the\s+)?(?:message|reply|note)|reply\s+to|comment\s+on|"
        r"on\s+(?:my|your|their|the\s+user'?s)\s+behalf|in\s+(?:my|your|their|the\s+user'?s)\s+name|as\s+(?:me|you|the\s+user)\b|"
        r"request(?:er|ed\s+by)|asked\s+by|(?:person|people)'?s?\s+request|(?:pr|pull\s+request|draft)\s+review"
        r"|review\s+of\s+(?:the\s+)?(?:draft\s+)?(?:pr|pull\s+request)(?:\s*#\s*\d+)?)",
        re.I)),
    ("credential, money or a step only the user holds", re.compile(
        r"\$|\b(?:credentials?|token|password|secret|api[- ]?key|ssh[- ]?key|log\s?-?in|sign\s?-?in|2fa|mfa|"
        r"device\s+code|budget|spend\w*|usd|dollars?|funds?|billing|paid|pay(?:ment)?|purchase|buy|credits?|"
        r"physical\w*|on[- ]site|in\s+person|cable|unplug|reseat|power\s+button|(?:your|their)\s+(?:own\s+)?(?:laptop|phone))\b",
        re.I)),
    ("a charter Restriction", re.compile(r"\b(?:restriction\w*|forbid\w*|prohibit\w*|binding)\b", re.I)),
    ("a pause or hold the user set", re.compile(
        rf"\b(?:paused|held|stopped|frozen)\s+by\s+{_U}\b|\b{_U}\s+(?:paused|froze|asked\s+(?:me\s+|us\s+)?to\s+"
        r"(?:pause|hold|stop|wait|freeze))\b|\b(?:user'?s|your)\s+(?:own\s+)?(?:pause|hold|freeze)\b|\bcode\s+freeze\b", re.I)),
)


# A people cue said in the negative ("no other tenant", "not in use") names no one it could affect.
_NEGATED = re.compile(r"\b(?:no|without|not\s+an?y?)\s+(?:other\s+)?(?:tenants?|users?|people|one\s+else|"
                      r"other\s+projects?)\b|\bnobody\s+else\b|\bnot\s+(?:in\s+use|used\s+by\s+\w+)", re.I)


# A cite said in the negative ("no longer waiting for your ok", "never held for the user's word").
_DENIED = re.compile(r"\b(?:no|not|never|without|no\s+longer|instead\s+of|rather\s+than)\b[\w\s']{0,20}$", re.I)


def gate(text: str) -> str | None:
    """The kind of real user gate `text` names (GATES), or None."""
    said = _NEGATED.sub(" ", (text or "").replace("’", "'"))
    for name, rx in GATES:
        if rx.search(said):
            return name
    try:
        from .coordinator import _names_needs_user   # a setting only the user may change (spend, push, merge)
        if _names_needs_user(said):
            return "a setting only the user may change"
    except Exception:
        pass
    return None


def finding(text: str, cite: bool = True, rx: re.Pattern = CITE) -> str | None:
    """The cited wording when `text` holds work for the user (`rx`; or, with `cite` False, whatever it
    says) and names no real gate; None when it is quiet."""
    said = " ".join((text or "").replace("’", "'").split())
    m = next((m for m in rx.finditer(said) if not _DENIED.search(said[max(0, m.start() - 36):m.start()])), None)
    if (cite and not m) or not said or gate(said):
        return None
    return m.group(0) if m else said[:80]


def _fp(source: str, ident, text: str) -> str:
    return hashlib.sha256(f"{source}:{ident}:{' '.join((text or '').split())}".encode()).hexdigest()[:16]


def _clip(s: str, n: int = 200) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _waiting_for(t: dict) -> str:
    """A waiting task's hand-off `waiting_for`, or ''."""
    if t.get("status") != "waiting":
        return ""
    try:
        res = json.loads(t.get("result") or "{}")
    except (TypeError, ValueError):
        return ""
    return str(res.get("waiting_for") or "") if isinstance(res, dict) else ""


def scan(db, now: float | None = None) -> list[dict]:
    """Every current finding: {source, id, fp, text, cited}."""
    from .askrethink import _cites
    from .settinghold import held_key
    from .waits import _held_ask
    now = time.time() if now is None else now
    asks = {a["id"]: a for a in db.q("SELECT id, text, ref FROM messages WHERE direction='out' AND kind='ask' "
                                     "AND handled=0 ORDER BY id")}

    def blocking(a) -> str:
        ref = a["ref"] or ""
        return ref[len("blocking:"):] if ref.startswith("blocking:") else ""

    user_only = [aid for aid, a in asks.items() if blocking(a) in USER_ONLY_ASKS]
    out = []
    for t in db.q("SELECT id, status, labels, blocked_reason, result FROM tasks WHERE status IN ('blocked','waiting') "
                  "ORDER BY id"):
        t = dict(t)
        why = " ".join(x for x in (t["blocked_reason"] or "", _waiting_for(t)) if x)
        if not why or held_key(t) or _held_ask(t) in user_only or any(_cites(why, aid) for aid in user_only):
            continue
        if (cited := finding(why)):
            out.append({"source": "task", "id": t["id"], "text": why, "cited": cited})
    for aid, a in asks.items():
        if blocking(a) in CHECKED_ASKS and (cited := finding(a["text"], cite=False)):
            out.append({"source": "ask", "id": aid, "text": a["text"], "cited": cited})
    for r in db.q("SELECT id, ts, text FROM messages WHERE direction='out' AND kind='reply' AND ts>=? ORDER BY id",
                  (now - REPLY_WINDOW_S,)):
        if (cited := finding(r["text"], rx=HOLD)):
            out.append({"source": "reply", "id": r["id"], "text": r["text"], "cited": cited, "ts": r["ts"]})
    # A throughput-class mute whose owner is 'outside' (its `why` says only the user or an outside
    # party can clear it, per throughmute.vet) is the same self-blocking when its `why` names no
    # GATES entry and cites no user message or ask: a wait on another's word for what is the
    # coordinator's own decision. A task- or probe-owned mute is not (review_lines flags it).
    from .project import USER_CITE
    from .screen import MUTES_KEY
    for m in db.kv(MUTES_KEY, []) or []:
        if float(m.get("until") or 0) <= now:   # expired: not a mute in force any more
            continue
        o = (m.get("throughput") or {}).get("owner") or {}
        if "outside" not in o:
            continue
        w = o.get("outside") or ""
        if not w or USER_CITE.search(w) or any(_cites(w, aid) for aid in user_only):
            continue   # a cite of the user's own message or ask: a user pause stays until their word
        if (cited := finding(w, cite=False)) is None:
            continue   # its why names a GATES entry, or a setting only the user may change
        out.append({"source": "mute", "id": f"{m.get('source')} {m.get('match')!r}", "text": w, "cited": cited})
    for f in out:
        f["fp"] = _fp(f["source"], f["id"], f["text"])
    return out


def text(found: list[dict]) -> str:
    names = {"task": "task #{}", "ask": "ask {}", "reply": "your reply (message {})",
             "mute": "observation_mute {} (a hold on the user or an outside party that names no gate)"}
    lines = [f"{names[f['source']].format(f['id'])}: \"{_clip(f['text'])}\" (cites: {f['cited']})"
             for f in found[:MAX_LISTED]]
    more = f" (+{len(found) - MAX_LISTED} more)" if len(found) > MAX_LISTED else ""
    return ("Self-blocking: work is held for the user's word on a decision that is yours. "
            + "; ".join(lines) + more + ". None names a step that could affect people badly, speaks as the user, "
            "needs a credential, money or a step only the user holds, changes a charter Restriction or lifts a "
            "pause the user set. Decide it now: pick the least-disruptive option, act (requeue or re-scope the "
            "task, resolve the ask), memory_add kind decision with the reason, notify low saying what you chose "
            "and that a reply reverses it. If one really is such a gate, say which in summary.")


def raise_due(db, now: float | None = None) -> list[dict]:
    """Queue one KIND event listing each finding not raised before; returns those findings."""
    now = time.time() if now is None else now
    found = scan(db, now)
    with db.tx():
        raised = {k: v for k, v in (db.kv(RAISED_KEY, {}) or {}).items()
                  if isinstance(v, (int, float)) and now - float(v) < KEEP_S}
        last = db.kv(REPLY_RAISED_KEY, 0)
        last = float(last) if isinstance(last, (int, float)) else 0.0
        # A reply raises at most once per window, never one posted in the window after an event.
        new = [f for f in found if f["fp"] not in raised
               and (f["source"] != "reply" or float(f.get("ts") or 0) >= last + REPLY_WINDOW_S)]
        if new:
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
                 (now, "daemon", KIND, "normal", text(new),
                  json.dumps({"findings": [{k: f[k] for k in ("source", "id", "cited")} for f in new]}), "queued"))
            raised.update({f["fp"]: now for f in new})
            if any(f["source"] == "reply" for f in new):
                db.set_kv(REPLY_RAISED_KEY, now)
        if new or raised != (db.kv(RAISED_KEY, {}) or {}):
            db.set_kv(RAISED_KEY, raised)
    return new
