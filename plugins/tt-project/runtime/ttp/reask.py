# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Re-ask gate: an ask whose subject the user already settled is refused, model-free.

Before an ask_user goes out, `settled_by` compares it with what is already settled, within
coordinator.reask_window_days (default WINDOW_DAYS; 0 turns the gate off):
- asks resolved with a linked user answer (unblock.RESOLVED_KEY; a 'decide it yourself' answer too),
- the user's own verbatim messages that answer or correct something (the relayer's context is
  dropped): a message linked to an ask (a '#<id>:' prefix, or recorded as an ask's answer) counts
  whole; any other message only by its sentences that carry a decision or correction marker
  (DECIDED: do not, never, keep, stop, instead, yes, decide yourself...). A plain request ('Set up
  X') answers nothing, so it never refuses the follow-up ask it leads to; a question, or an 'ask me
  first' sentence, settles nothing,
- memory entries of kind decision, preference, restriction or fact that cite a user message or are a
  `Correction (user msg #id)` fact,
- the charter's Policies and Goals items (no age: the charter is current).
A restriction-change ask is compared only with resolved asks, the user's messages and Correction
facts: naming a charter Restriction never refuses it; only the user's word on that change does.

Match: each record is cut into sentences; an ask matches a sentence by content words (lower case,
punctuation and STOP words dropped, a light suffix stem) when they share at least MIN_SHARED words
and either their Jaccard is at least JACCARD, or they share a hard anchor (a task id, PR, machine or
resource alias, config key) and the Jaccard is at least ANCHORED_JACCARD with the record covering at
least CONTAINS of the ask's words and MIN_SHARED shared words besides the anchors. Asks and records
that both carry anchors but share none are about different things and never match; an anchor alone
with low word overlap is not enough. A new question about a settled subject never matches: when one of
the ask's question clauses (a '?' sentence, from its last ':', ';', dash or ', which/should...'
lead-in) brings at least MIN_NEW words the record lacks and the record holds less than CONTAINS of
its words. A question that is the whole ask never passes this way: a Jaccard match already holds
at least CONTAINS of the ask's words. When unsure the ask goes out: the thresholds favour a missed
re-ask over a false refusal.
"""
from __future__ import annotations

import re
import time

WINDOW_DAYS = 30.0
MIN_SHARED = 3
JACCARD = 0.5
ANCHORED_JACCARD = 0.35
CONTAINS = 0.5
MIN_NEW = 2   # new words a question clause needs to count as a new question
QUOTE = 160   # chars of the settling answer quoted in a refusal
MEMORY_KINDS = ("decision", "preference", "restriction", "fact")

STOP = set("""a an the of to for and or nor in on at by with from into onto as about over under after before
during up down out off via vs per than then so if else whether is are was were be been being am do does did
done doing have has had having it its this that these those there here what which who whom whose how when
where why i me my we us our you your he she they them their it's that's i'm we're let's should could would
can may might will shall must need needs want wants like please ok okay now again still also just yet only
even very too more most less any some each every all both either neither one not no yes go ahead get make
user ask asked asks asking question questions answer reply tell know think sure fine right new same msg
message correction""".split())

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_]*(?:[.:/#-][a-z0-9_]+)*")
_SENT = re.compile(r"(?<=[.!?;])\s+|\n+")
_LEAD = re.compile(r"[:;]\s|\s[-\u2013\u2014]+\s|,\s+(?=(?:which|what|when|where|who|how|should|shall|can|could|"
                   r"may|do|does|is|are|will|would)\b)", re.I)   # a lead-in before the question itself
_PR = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)|\b([\w.-]+/[\w.-]+)#(\d+)\b", re.I)
_TASK = re.compile(r"(?<![\w/])#(\d+)\b")
_NOT_TASK = re.compile(r"\b(?:msg|message|ask|issue|note)s?\s*#\d+")   # a message, ask or issue id is no task
_NOT_KEY = {"md", "json", "py", "sh", "txt", "yaml", "yml", "toml", "log", "com", "org", "io", "net", "g", "e"}
_CONFIG = re.compile(r"\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*\b")
_ALIAS = re.compile(r"\b[a-z][a-z0-9_-]*\d[a-z0-9_-]*\b")
_USER_MSG = re.compile(r"\buser (?:msg|message)s? #\d+", re.I)
_ASK_FIRST = re.compile(r"\b(?:ask|check with|confirm with|tell|ping) me\b|\bask (?:first|before)\b", re.I)
_CHARTER_SECTIONS = ("policies", "goals")
_LINKED = re.compile(r"^\s*#\d+\s*:")   # '#<id>: ...', the user's answer to ask #id
DECIDED = re.compile(r"\b(?:do not|does not|don't|dont|never|keep|stop|no(?! (?:rush|hurry))|"
                     r"not(?! sure)|instead|rather|already (?:said|told|answered|decided)|"
                     r"decide (?:it |that |this )?yourself|your call|yes|approved?|"
                     r"denied|declined?|rejected?|leave it|as is)\b", re.I)


def window_days(cfg: dict | None) -> float:
    try:
        return max(0.0, float(((cfg or {}).get("coordinator") or {}).get("reask_window_days", WINDOW_DAYS)))
    except (TypeError, ValueError):
        return WINDOW_DAYS


def _stem(w: str) -> str:
    """delete, deletes, deleted, deleting: delet."""
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and not w.endswith("ss"):
            w = w[:-len(suf)]
            break
    return w[:-1] if w.endswith("e") and len(w) > 4 else w


def words(text: str) -> set[str]:
    """Content words: lower case, STOP words and punctuation dropped, lightly stemmed."""
    return {_stem(w) for w in _TOKEN.findall((text or "").lower().replace("’", "'")) if w not in STOP}


def anchors(text: str) -> set[str]:
    """Hard anchors: PRs (owner/repo#n), task ids (#n), config keys (a.b) and machine or resource
    aliases (a word mixing letters and digits, such as box-a1)."""
    low = (text or "").lower()
    out = {f"pr:{(m.group(1) or m.group(3)).lower()}#{m.group(2) or m.group(4)}" for m in _PR.finditer(low)}
    rest = _PR.sub(" ", low)
    rest = re.sub(r"https?://\S+", " ", rest)
    rest = _NOT_TASK.sub(" ", rest)
    out |= {f"task:{n}" for n in _TASK.findall(rest)}
    out |= {f"key:{k}" for k in _CONFIG.findall(rest)
            if k.rsplit(".", 1)[1] not in _NOT_KEY and all(len(x) >= 2 for x in k.split("."))}
    out |= {f"alias:{a}" for a in _ALIAS.findall(rest) if len(a) >= 3}
    return out


def _questions(ask: str) -> list[set[str]]:
    """Content words of each question clause of the ask: a '?' sentence from its last lead-in on."""
    return [words(_LEAD.split(s)[-1]) for s in re.split(r"(?<=[.!?])\s+|\n+", ask or "")
            if s.rstrip().endswith("?")]


def matches(ask: str, record: str) -> bool:
    a, r = words(ask), words(record)
    if any(len(q - r) >= MIN_NEW and len(q & r) < CONTAINS * len(q) for q in _questions(ask)):
        return False   # a new question about the subject, not a re-ask
    shared = a & r
    if len(shared) < MIN_SHARED:
        return False
    aa, ra = anchors(ask), anchors(record)
    if aa and ra and not aa & ra:
        return False
    jac = len(shared) / len(a | r)
    if jac >= JACCARD:
        return True
    plain = {w for w in shared if not re.search(r"\d|[.:/#]", w)}   # an alias, id or key is no word here
    return bool(aa & ra) and jac >= ANCHORED_JACCARD and len(shared) / len(a) >= CONTAINS \
        and len(plain) >= MIN_SHARED


def _sentences(text: str) -> list[str]:
    return [s.strip(" -*\t") for s in _SENT.split(text or "") if s.strip(" -*\t")]


def _line(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= QUOTE else text[:QUOTE - 3].rstrip() + "..."


def _hit(ask: str, text: str, settling: bool = True, decided: bool = False) -> str | None:
    """The first sentence of `text` that matches `ask`. With `settling`, a question or an
    'ask me first' sentence never settles anything; with `decided`, only a sentence with a
    decision or correction marker does."""
    for s in _sentences(text):
        if settling and (s.endswith("?") or _ASK_FIRST.search(s)):
            continue
        if decided and not DECIDED.search(s):
            continue
        if matches(ask, s):
            return s
    return None


def _resolved(db, ask: str, since: float, question) -> str | None:
    from .unblock import RESOLVED_KEY
    rec = db.kv(RESOLVED_KEY, {}) or {}
    for aid, r in sorted(rec.items(), key=lambda kv: -int(kv[0])):
        if not isinstance(r, dict) or float(r.get("ts") or 0) < since or not (r.get("messages") or r.get("auto")):
            continue
        row = db.one("SELECT text FROM messages WHERE id=? AND kind='ask' AND direction='out'", (int(aid),))
        if not row or not (matches(ask, question(row["text"])) or _hit(ask, question(row["text"]), False)):
            continue
        if r.get("auto") and not r.get("messages"):   # unanswered: it resolved to its recommendation
            return f'ask #{aid}, auto-resolved unanswered to its recommendation: "{_line(str(r["auto"]))}"'
        ids = [int(m) for m in r["messages"]][:50]
        said = db.q(f"SELECT id, text, context FROM messages WHERE direction='in' AND kind='user' "
                    f"AND id IN ({','.join('?' * len(ids))}) ORDER BY id", ids)
        from .relay import split
        said = [(m["id"], split(m["text"] or "", m["context"])[0]) for m in said]
        said = [m for m in said if m[1].strip()]
        if not said:
            continue
        mid, text = next((m for m in said if re.search(rf"#{aid}\b", m[1])), said[0])
        return f'ask #{aid}, answered in user msg #{mid}: "{_line(text)}"'
    return None


def settled_by(p, cfg: dict | None, ask: str, change: bool = False, now: float | None = None,
               question=lambda t: t) -> str | None:
    """What already settled `ask` ('ask #n ...', 'user msg #n ...', 'memory [name] ...' or 'the
    charter's ... item', with the answer quoted on one line), or None. `change`: a restriction-change
    ask, compared only with resolved asks, the user's messages and Correction facts. `question`
    strips the gate's notes from a sent ask's text."""
    days = window_days(cfg)
    if not days or not words(ask):
        return None
    since = (time.time() if now is None else now) - days * 86400
    db = p.db
    found = _resolved(db, ask, since, question)
    if found:
        return found
    from .relay import split
    from .unblock import RESOLVED_KEY
    answers = {int(m) for r in (db.kv(RESOLVED_KEY, {}) or {}).values() if isinstance(r, dict)
               for m in r.get("messages") or [] if str(m).isdigit()}
    for m in db.q("SELECT id, text, context FROM messages WHERE direction='in' AND kind='user' "
                  "AND COALESCE(provenance,'')!='system' AND ts>=? ORDER BY id DESC", (since,)):
        said = split(m["text"] or "", m["context"])[0]
        s = _hit(ask, said, decided=not (m["id"] in answers or _LINKED.match(said)))
        if s:
            return f'user msg #{m["id"]}: "{_line(s)}"'
    from .project import CORRECTION_PREFIX, _stamp_ts
    for e in reversed(p._memory_entries()):
        body, correction = e["body"], e["body"].startswith(CORRECTION_PREFIX)
        if e["kind"] not in MEMORY_KINDS or _stamp_ts(e["created"]) < since:
            continue
        if not (correction or (not change and _USER_MSG.search(body))):
            continue
        s = _hit(ask, body)
        if s:
            return f'memory [{e["name"]}]: "{_line(s)}"'
    if change:
        return None
    try:
        charter = p.charter_path.read_text()
    except OSError:
        return None
    from .prompts import charter_sections
    for head, body in charter_sections(charter):
        name = head[3:].strip()
        if not name.lower().startswith(_CHARTER_SECTIONS):
            continue
        s = _hit(ask, "\n".join(body))
        if s:
            return f'the charter\'s {name.split("(")[0].strip()} item: "{_line(s)}"'
    return None
