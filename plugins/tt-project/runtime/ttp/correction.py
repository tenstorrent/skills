# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Corrections: a user or chat message that points out something the project should have noticed or
done itself. The coordinator fixes that case and adds a `harness` task for the class of failure (the
detector, rule or default that would have caught it), so the next case is not left to the user.

Model-free, no model calls. At a coordinator turn's start the daemon flags each new inbound message
that is a likely correction (`flag`): one matching a small generic phrase list (PHRASES), one
repeating an earlier instruction (`repeats`: a sentence of REPEAT_WORDS+ words matching an inbound
sentence of the last REPEAT_S, exactly once normalized or nearly over the last REPEAT_NEAR; or an
instruction of any length sharing an anchor (`anchors`: a model name, an effort level, a config key,
a task id) and at least half its content words with one), or a message that answers or rejects
earlier output (`answers`: it quotes it or contradicts it; never a new instruction or a question)
while something says the project is stuck (`open_stall`: a stall event or a Course check line; a
correction still without a class fix is not one, or it would flag every later message, and neither is
Resource trouble, a machine fault rather than a miss). A relayer's own findings (kind 'finding') and a
relay re-send (`resends`: the same words delivered twice within RESEND_S, or relayed again with
context) are never flagged: neither is the user saying something again. The digest tags the message
`[likely correction: <why>]`. After the turn (`settle`), each flagged message is recorded with its
class fix: a `harness` task (or one titled `upstream: ...`) the turn added, or the turn's `summary`
naming `correction #<id>` with its reason. A repeated request (`request`: 'I told you', 'N times',
'asked ... again', 'why was nothing done') is one the user says was already made and answered with no
change: its tag and Course check line ask for a queued task with a measurable change (any task kind
but a question or plan counts as its fix) or `correction #<id>: <why not>`; an explanation alone is no
fix. A second reason ('none') within REFUSE_S for the same class
(a shared anchor, or a repeat of the same first message) is refused: it stays without a class fix.
One with neither, or refused, gets a Course check line in the next digest (a refused one until a
harness task comes, for up to a day; `digest_lines`). The daily review's efficiency audit lists each
correction of the last 24 h and the task that fixed its class, or none, and counts the repeated
instructions (`audit_line`).

A worker's result rows that name a model or effort (`metrics`, or a `tried`, `cells` or `results` row
with its `run`) carry what that run used, read from its runs row (`check_claims`); a mismatch is
flagged in the result and the hand-off's digest notes."""
from __future__ import annotations

import re
import time

from .db import DB

KEY = "corrections"   # kv: [{"msg", "ts", "why", "fix", "shown"}], newest last
KEEP_S = 7 * 86400
KEEP_N = 200
DAY = 86400
# Phrases of a message pointing out a miss. Generic and short: a false flag costs one tag and at most
# one Course check line; a missed one leaves the class gap open.
PHRASES = ("why didn't", "why did nobody", "why did no one", "why hasn't", "why haven't", "why wasn't",
           "keeps failing", "keep failing", "kept failing", "still failing", "stuck for", "you missed",
           "nobody noticed", "no one noticed", "didn't notice", "failed to notice", "failing to notice",
           "should have noticed", "should have caught", "lack of progress", "no progress",
           "i already asked", "i already told", "i told you", "as i said", "many times")
# A repeated request: the user says a request was already made and answered without a change.
_ASKED = r"(?:asked|asking|told|telling|said|saying|requested|mentioned|reminded)"
_REQUEST = re.compile(
    r"\bi(?: have| had|'ve)? (?:already )?(?:told|asked) you\b"
    rf"|\b{_ASKED}\b[^.?!\n]{{0,60}}\b(?:\d+|many|several|multiple|countless)\s+times\b"
    r"|\b(?:a (?:million|hundred|thousand|dozen)|so many|countless) times\b"
    r"|\bwhy (?:was|is|has|did) nothing\b|\bnothing (?:was|has been|got|is being) done\b"
    rf"|\b{_ASKED}\b[^.?!\n]{{0,40}}\bagain\b|\b(?:once|yet) again\b")
REQUEST = "repeated request"
NOT_FIX_KINDS = ("question", "plan")   # a repeated request's fix is a queued change, not more analysis
# Event kinds of a stall detector (no progress on a task or the project). Any such event not yet
# handled makes the next user message a likely correction.
STALL_KINDS = ("no_progress", "stall")
FIX_KINDS = ("harness",)
# A repeated instruction: a user round trip and a standing rule that did not reach the prompts. A
# sentence this short ('yes go ahead') repeats by nature; a near match is token-set Jaccard >= REPEAT_J
# (0.75: one word changed in 8 is 7/9), checked against only the newest REPEAT_NEAR sentences, and at
# most REPEAT_ROWS messages are read, so a flag costs the same however long the chat is.
REPEAT_WORDS = 5
REPEAT_S = 7 * 86400
REPEAT_J = 0.75
REPEAT_NEAR = 400
REPEAT_ROWS = 1000
RESEND_S = 60   # the same words again this soon are one message delivered twice, not a repeat
# Anchors: what makes a short instruction specific enough that its repeat is one ('yes go ahead' has
# none). An anchored instruction repeats an earlier one sharing an anchor and at least half its content
# words (ANCHOR_SHARE), checked against at most REPEAT_NEAR earlier clauses per anchor.
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
_LEVEL = r"(minimal|low|medium|high|xhigh|max|extra[- ]high)"
_EFFORT = re.compile(rf"\b{_LEVEL}[- ]effort\b|\beffort\s*(?:[=:]|of|at|to|is)?\s*{_LEVEL}\b")
_MODEL = re.compile(r"\b(opus|sonnet|haiku|fable|codex|gemini|composer|gpt-?\d[\w.]*|o[134](?:-mini)?)\b")
_KEY = re.compile(r"\b[a-z][a-z0-9_]+(?:\.[a-z][a-z0-9_]+)+\b")   # checks.wrap_cmd; e.g. and i.e. are too short
NOT_KEYS = frozenset("py md json jsonl sh txt yaml yml toml js ts html css log cfg ini csv com org net io dev "
                     "ai".split())   # a file name or a domain, not a config key
_TASK = re.compile(r"(?:#|\btask\s+#?)(\d+)\b")
ANCHOR_SHARE = 0.5
STOP = frozenset("a an the to of for on in at and or is are be it its this that these those with by from as "
                 "please pls now just again also do does you we i me my our your all any then than so but if "
                 "can could would should will it's that's".split())
_CLAUSE = re.compile(r"(?<=[?!;\n])|\.(?:\s+|$)")   # a dot inside a key or version does not end one
# A message that answers or rejects earlier output: it quotes it (a '>' line, or a quoted span of
# QUOTE_WORDS+ words an outbound message of the last REPEAT_S had) or contradicts it (REJECTS in a
# clause that is not a question, or a clause that is only 'no'). Only such a message counts as a
# correction by arriving during an open stall: a new instruction or a question there is not one.
REJECTS = ("that's wrong", "that is wrong", "is wrong", "was wrong", "not true", "not what i", "not right",
           "incorrect", "that's not", "that is not", "you said", "you claimed", "you reported", "you wrote",
           "but you", "no, ", "nope", "actually,")
QUOTE_WORDS = 3
_QUOTED = re.compile(r'"([^"\n]{8,300})"|“([^”\n]{8,300})”')
REFUSE_S = 7 * 86400   # a second 'none' for the same class this soon is refused
CLAIM_KEYS = ("tried", "cells", "results", "rows")   # hand-off fields whose dict rows may name a run


def phrase(text: str) -> str:
    """The first PHRASES entry in `text`, or ''."""
    low = " ".join(str(text or "").lower().replace("’", "'").split())
    return next((x for x in PHRASES if x in low), "")


def request(text: str) -> str:
    """The words of `text` saying a request was already made and answered without a change, or ''."""
    low = " ".join(str(text or "").lower().replace("’", "'").split())
    m = _REQUEST.search(low)
    return m.group(0) if m else ""


def sentences(text: str) -> list[tuple[str, ...]]:
    """`text`'s sentences of REPEAT_WORDS+ words, each lowercased to its words only."""
    out = []
    for part in re.split(r"[.!?;\n]+", str(text or "").lower().replace("’", "'")):
        words = tuple(re.findall(r"[a-z0-9]+(?:'[a-z]+)?", part))
        if len(words) >= REPEAT_WORDS:
            out.append(words)
    return out


def _effort(level: str) -> str:
    return "xhigh" if level.startswith("extra") else level


def anchors(text: str) -> frozenset:
    """The anchors `text` names: 'model:<family>', 'effort:<level>', 'key:<dotted.key>', '#<task id>'."""
    low = str(text or "").lower().replace("’", "'")
    out = {f"effort:{_effort(next(g for g in m.groups() if g))}" for m in _EFFORT.finditer(low)}
    out |= {f"model:{m.group(1)}" for m in _MODEL.finditer(low)}
    out |= {f"key:{m.group(0)}" for m in _KEY.finditer(low) if m.group(0).rsplit(".", 1)[1] not in NOT_KEYS}
    out |= {f"#{m.group(1)}" for m in _TASK.finditer(low)}
    return frozenset(out)


def clauses(text: str) -> list[tuple[tuple[str, ...], frozenset, bool]]:
    """`text`'s clauses of any length: (its words, its anchors, whether it is a question)."""
    out = []
    for part in _CLAUSE.split(str(text or "").replace("’", "'")):
        words = tuple(re.findall(r"[a-z0-9]+(?:'[a-z]+)?", part.lower()))
        if words:
            out.append((words, anchors(part), part.rstrip().endswith("?")))
    return out


def _content(words: tuple) -> frozenset:
    return frozenset(w for w in words if w not in STOP)


def _near(a: frozenset, b: frozenset) -> bool:
    return len(a & b) >= REPEAT_J * len(a | b)


def repeats(db: DB, msgs: list[dict], now: float | None = None) -> dict[int, tuple[int, str]]:
    """{message id: (first id, sentence)} for the inbound messages in `msgs` with a sentence an earlier
    inbound message of the last REPEAT_S already had (or an anchored instruction of any length one
    already gave): the first message that said it, and the sentence as first said."""
    now = time.time() if now is None else now
    new = sorted((m for m in msgs if m.get("direction", "in") == "in" and m.get("kind", "user") != "finding"
                  and (m.get("text") or "").strip()), key=lambda m: int(m["id"]))
    if not new:
        return {}
    rows = db.q("SELECT id, ts, text FROM messages WHERE direction='in' AND kind!='finding' AND ts>=? AND id<? "
                "ORDER BY id DESC LIMIT ?", (now - REPEAT_S, int(new[-1]["id"]), REPEAT_ROWS))[::-1]
    first: dict[tuple, tuple[int, str]] = {}   # normalized sentence: (first id, its words)
    last: dict[tuple, float] = {}              # normalized sentence: when it was last said
    recent: list[tuple[frozenset, tuple]] = []
    by_anchor: dict[str, list[tuple[int, float, frozenset, str]]] = {}   # anchor: (id, ts, content, clause)
    out, i = {}, 0
    for m in new:
        mid = int(m["id"])
        while i < len(rows) and int(rows[i]["id"]) < mid:   # earlier messages only
            for s in sentences(rows[i]["text"]):
                if s not in first:
                    first[s] = (int(rows[i]["id"]), " ".join(s))
                    recent.append((frozenset(s), s))
                last[s] = float(rows[i]["ts"] or 0)
            for words, anc, ask in clauses(rows[i]["text"]):
                for a in anc if not ask else ():
                    got = by_anchor.setdefault(a, [])
                    got.append((int(rows[i]["id"]), float(rows[i]["ts"] or 0), _content(words), " ".join(words)))
                    del got[:-REPEAT_NEAR]
            i += 1
        recent = recent[-REPEAT_NEAR:]
        ts = float(m.get("ts") or now)
        for s in sentences(m.get("text") or ""):
            hit = s if s in first else None
            if not hit:
                toks = frozenset(s)
                hit = next((o for t, o in recent if _near(toks, t)), None)   # oldest first
            if hit and ts - last[hit] > RESEND_S:   # sooner: one message delivered twice (`resends`)
                out[mid] = first[hit]
                break
        if mid in out:
            continue
        for words, anc, ask in clauses(m.get("text") or ""):
            said = _content(words)
            if ask or not anc or len(said) < 2:
                continue
            cands = sorted({c for a in anc for c in by_anchor.get(a, ())})   # oldest first
            hit = next((c for c in cands if len(said & c[2]) >= ANCHOR_SHARE * len(said) and ts - c[1] > RESEND_S),
                       None)
            if hit:
                out[mid] = (hit[0], hit[3])
                break
    return out


def _same(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def resends(db: DB, msgs: list[dict], now: float | None = None) -> set[int]:
    """The ids of the messages in `msgs` that a relay sent again, not the user repeating them: words
    (case and whitespace aside) an earlier inbound message already had within RESEND_S (one message
    delivered twice), or, for a relayed message (with a relayer's context), one of the last REPEAT_S
    from the same chat (the same words passed on again to carry new context)."""
    now = time.time() if now is None else now
    out = set()
    for m in msgs:
        said = _same(m.get("text") or "")
        if m.get("direction", "in") != "in" or m.get("kind", "user") != "user" or not said:
            continue
        ts, relayed = float(m.get("ts") or now), bool((m.get("context") or "").strip())
        rows = db.q("SELECT ts, chat, text FROM messages WHERE direction='in' AND kind='user' AND ts>=? AND id<? "
                    "ORDER BY id DESC LIMIT ?", (min(now, ts) - (REPEAT_S if relayed else RESEND_S), int(m["id"]),
                                                 REPEAT_ROWS))
        if any(_same(r["text"]) == said and (float(r["ts"]) >= ts - RESEND_S or relayed and r["chat"] == m.get("chat"))
               for r in rows):
            out.add(int(m["id"]))
    return out


def _records(db: DB) -> list[dict]:
    got = db.kv(KEY, []) or []
    return [r for r in got if isinstance(r, dict) and str(r.get("msg", "")).isdigit()]


def _pending(db: DB, now: float) -> list[dict]:
    """Recorded corrections with no class fix, not shown yet (or refused a second 'none'), younger than a
    day, with no harness task added since they arrived."""
    out = []
    for r in _records(db):
        if r.get("fix") or r.get("shown") and not r.get("refused") or now - float(r.get("ts") or 0) > DAY:
            continue
        if _later_fix(db, float(r.get("ts") or 0)):
            continue
        out.append(r)
    return out


def _later_fix(db: DB, since: float) -> str:
    row = db.one(f"SELECT id FROM tasks WHERE created>=? AND (kind IN ({','.join('?' * len(FIX_KINDS))}) "
                 "OR lower(title) LIKE 'upstream:%') ORDER BY id LIMIT 1", (since, *FIX_KINDS))
    return f"#{row['id']}" if row else ""


def open_stall(db: DB, cfg: dict | None, now: float | None = None) -> str:
    """What says the project is stuck right now, or ''. Cheapest first."""
    now = time.time() if now is None else now
    if db.one(f"SELECT 1 FROM events WHERE status!='handled' AND kind IN ({','.join('?' * len(STALL_KINDS))})",
              STALL_KINDS):
        return "open stall"
    from . import course
    if course.digest_lines(db, cfg, now)[0]:
        return "open Course check"
    return ""


def answers(db: DB, m: dict, now: float | None = None) -> str:
    """How message `m` answers or rejects earlier output ('quotes earlier output', 'contradicts it'),
    or '' for a new instruction, a question or anything else."""
    now = time.time() if now is None else now
    text = str(m.get("text") or "").replace("’", "'")
    if any(line.lstrip().startswith(">") for line in text.splitlines()):
        return "quotes earlier output"
    spans = [_same(a or b) for a, b in _QUOTED.findall(text)]
    spans = [x for x in spans if len(x.split()) >= QUOTE_WORDS]
    if spans:
        outs = db.q("SELECT text FROM messages WHERE direction='out' AND ts>=? AND id<? ORDER BY id DESC LIMIT ?",
                    (now - REPEAT_S, int(m["id"]), REPEAT_NEAR))
        said = [_same(r["text"]) for r in outs]
        if any(x in o for x in spans for o in said):
            return "quotes earlier output"
    for part in _CLAUSE.split(text):
        low = " ".join(part.lower().split())
        if not low or low.endswith("?"):
            continue
        if re.fullmatch(r"(no|nope|wrong)\W*", low) or any(x in low + " " for x in REJECTS):
            return "contradicts it"
    return ""


def flag(db: DB, cfg: dict | None, msgs: list[dict], now: float | None = None) -> dict[int, str]:
    """{message id: why} for the inbound messages in `msgs` that are likely corrections. Rows without
    a `text` column (the daemon's turn-sizing query) get theirs read here."""
    out, stall, rep = {}, None, None
    bare = [int(m["id"]) for m in msgs if "text" not in m]
    if bare:
        full = {r["id"]: r for r in db.q(f"SELECT id, ts, direction, kind, chat, text, context FROM messages WHERE id IN "
                                         f"({','.join('?' * len(bare))})", bare)}
        msgs = [full.get(int(m["id"]), m) if "text" not in m else m for m in msgs]
    msgs = [dict(m) for m in msgs if m.get("kind", "user") != "finding"]
    again = resends(db, msgs, now)
    msgs = [m for m in msgs if int(m["id"]) not in again]
    for m in msgs:
        if m.get("direction", "in") != "in":
            continue
        if hit := request(m.get("text") or ""):
            out[int(m["id"])] = f"{REQUEST} '{hit[:40]}'"
            continue
        hit = phrase(m.get("text") or "")
        if hit:
            out[int(m["id"])] = f"phrase '{hit}'"
            continue
        if rep is None:
            rep = repeats(db, msgs, now)
        if int(m["id"]) in rep:
            out[int(m["id"])] = f"repeats #{rep[int(m['id'])][0]}"
            continue
        if stall is None:
            stall = open_stall(db, cfg, now)
        if stall and (how := answers(db, m, now)):
            out[int(m["id"])] = f"{stall}: {how}"
    return out


def tag(why: str, mid: int | None = None) -> str:
    if str(why).startswith(REQUEST):   # answered before with no change: this turn queues one or says why not
        return (f" [likely correction: {why}: queue a task with a measurable change or say "
                f"`correction #{mid if mid is not None else '<id>'}: <why not>` in summary; an explanation alone "
                "is no answer]")
    return f" [likely correction: {why}]"


def class_fix(db: DB, actions: list, summary: str, mid: int, since: float, why: str = "") -> str:
    """The turn's class fix for message `mid`: '#<task>' it added, 'reason' its summary gave, or ''.
    For a repeated request (`why`) any queued change counts: a task of a kind not in NOT_FIX_KINDS."""
    req = str(why or "").startswith(REQUEST)
    titles = [str(a.get("title") or "") for a in actions if isinstance(a, dict) and a.get("type") == "task_add"
              and (a.get("kind") in FIX_KINDS or str(a.get("title") or "").strip().lower().startswith("upstream:")
                   or (req and str(a.get("kind") or "work") not in NOT_FIX_KINDS))]
    if titles:
        row = db.one(f"SELECT id FROM tasks WHERE created>=? AND title IN ({','.join('?' * len(titles))}) "
                     "ORDER BY id LIMIT 1", (since, *titles))
        if row:
            return f"#{row['id']}"
    if re.search(rf"\bcorrection\s*#?{int(mid)}\b", summary or "", re.I):
        return "reason"
    return ""


def _class(why: str, text: str) -> list[str]:
    """A correction's class: its message's anchors, and for a repeat the message it repeats."""
    out = set(anchors(text))
    if m := re.fullmatch(r"repeats #(\d+)", str(why or "")):
        out.add(f"msg#{m.group(1)}")
    return sorted(out)


def _refuse(recs: list[dict], rec: dict, fix: str, now: float) -> str:
    """`fix` for `rec`, or '' when it is a reason ('none') and another correction of the same class
    already got one within REFUSE_S: then `rec` is marked refused with that one's id."""
    if fix != "reason":
        if fix:
            rec.pop("refused", None)
        return fix
    cls = set(rec.get("class") or [])
    prior = next((r for r in recs if r is not rec and r.get("fix") == "reason" and cls & set(r.get("class") or [])
                  and now - float(r.get("ts") or 0) <= REFUSE_S), None) if cls else None
    if not prior:
        return fix
    rec["refused"] = f"#{prior['msg']}"
    return ""


def settle(db: DB, note: dict, actions: list, summary: str, started: float, now: float | None = None) -> None:
    """After a coordinator turn: record its flagged messages with their class fix, and mark the
    correction lines its digest showed (those recorded before it started) as shown, with this turn's
    class fix if it made one. A second 'none' for one class within REFUSE_S is refused (_refuse)."""
    now = time.time() if now is None else now
    recs = _records(db)
    by_msg = {int(r["msg"]): r for r in recs}
    for r in recs:
        if not r.get("fix") and (not r.get("shown") or r.get("refused")) and float(r.get("ts") or 0) < started:
            r["shown"] = True
            r["fix"] = _refuse(recs, r, class_fix(db, actions, summary, int(r["msg"]), started, r.get("why") or "")
                               or _later_fix(db, float(r["ts"])), now)
    got = {int(k): v for k, v in (note.get("corrections") or {}).items()}
    texts = {int(r["id"]): r["text"] or "" for r in db.q(
        f"SELECT id, text FROM messages WHERE id IN ({','.join('?' * len(got))})", list(got))} if got else {}
    for mid, why in got.items():
        fix = class_fix(db, actions, summary, mid, started, str(why))
        if mid in by_msg:
            by_msg[mid]["fix"] = by_msg[mid].get("fix") or _refuse(recs, by_msg[mid], fix, now)
            continue
        rec = {"msg": mid, "ts": now, "why": str(why)[:80], "class": _class(str(why), texts.get(mid, "")),
               "shown": False}
        rec["fix"] = _refuse(recs, rec, fix, now)
        recs.append(rec)
        by_msg[mid] = rec
    recs = [r for r in recs if now - float(r.get("ts") or 0) <= KEEP_S][-KEEP_N:]
    db.set_kv(KEY, recs)


def digest_lines(db: DB, now: float | None = None) -> list[str]:
    """One Course check line for the corrections the turn that handled them gave no class fix, and one
    for those whose 'none' was refused; [] when there are none."""
    now = time.time() if now is None else now
    got = _pending(db, now)
    out = []
    asks = [r for r in got if not r.get("refused") and str(r.get("why") or "").startswith(REQUEST)]
    for r in asks:
        out.append(f"- repeated request without a change: user message #{r['msg']} repeats a request already "
                   "answered and got no queued task | queue a task with a measurable change, or say "
                   f"`correction #{r['msg']}: <why not>` in summary; an explanation alone is no answer")
    plain = [r for r in got if not r.get("refused") and r not in asks]
    if plain:
        ids = ", ".join(f"#{r['msg']}" for r in plain)
        one = plain[0]["msg"] if len(plain) == 1 else "<id>"
        out.append(f"- correction without a class fix: user message{'s' if len(plain) > 1 else ''} {ids} pointed "
                   "out a miss and got no `harness` task or `upstream:` note | add a `harness` task for the detector, "
                   f"rule or default that would have caught it, or say `correction #{one}: <why not>` in summary")
    refused = [r for r in got if r.get("refused")]
    if refused:
        out.append("- correction 'none' refused: " + ", ".join(f"#{r['msg']} (same class as {r['refused']})"
                                                              for r in refused)
                   + " got a reason instead of a class fix twice within 7 days | record the class fix or add a "
                   "`harness` task for it; another reason is not accepted")
    return out


def audit_line(db: DB, since: float) -> str:
    """The self-efficiency audit's line: each correction since `since` and its class fix, and the
    repeated instructions with the most repeated one; '' with none."""
    got = [r for r in _records(db) if float(r.get("ts") or 0) >= since]
    if not got:
        return ""
    def fixed(r: dict) -> str:
        fix = r.get("fix") or ""
        if not fix and r.get("refused"):
            return f"none (refused: same class as {r['refused']})"
        return f"class fix {fix}" if fix.startswith("#") else "none (reason given)" if fix == "reason" else "none"
    return (f"corrections from the user or a chat: {len(got)}, {sum(str(r.get('fix') or '').startswith('#') for r in got)} "
            "with a class fix: " + "; ".join(f"#{r['msg']} ({r.get('why')}) {fixed(r)}" for r in got[:8])
            + (f"; and {len(got) - 8} more" if len(got) > 8 else "") + _repeat_clause(db, since))


def repeat_summary(db: DB, since: float) -> dict:
    """The repeated instructions flagged since `since`: {count, top_sentence (<=60 chars, as first
    said), top_count, first_id}; count 0 and the rest empty or None with none."""
    got = [r for r in _records(db) if float(r.get("ts") or 0) >= since]
    by: dict[int, int] = {}
    for r in got:
        if m := re.fullmatch(r"repeats #(\d+)", str(r.get("why") or "")):
            by[int(m.group(1))] = by.get(int(m.group(1)), 0) + 1
    if not by:
        return {"count": 0, "top_sentence": "", "top_count": 0, "first_id": None}
    top, k = max(by.items(), key=lambda x: (x[1], -x[0]))
    later = [int(r["msg"]) for r in got if r.get("why") == f"repeats #{top}"][-1]
    text = {r["id"]: r["text"] for r in db.q("SELECT id, text FROM messages WHERE id IN (?,?)", (top, later))}
    pairs = [(len(set(a) & set(b)) / len(set(a) | set(b)), " ".join(a))
             for a in sentences(text.get(top, "")) for b in sentences(text.get(later, ""))]
    said = max(pairs)[1] if pairs else ""   # the sentence as first said
    return {"count": sum(by.values()), "top_sentence": said[:60], "top_count": k, "first_id": top}


def _repeat_clause(db: DB, since: float) -> str:
    """'; repeated instructions, 24 h: N (top: "<sentence>" xK, first #id)', or ''."""
    s = repeat_summary(db, since)
    if not s["count"]:
        return ""
    return (f"; repeated instructions, 24 h: {s['count']} (top: "
            + (f'"{s["top_sentence"]}" ' if s["top_sentence"] else "") + f"x{s['top_count']}, first #{s['first_id']})")


def _norm_effort(v) -> str:
    low = " ".join(str(v or "").lower().replace("_", "-").split())
    return _effort(low) if low in (*EFFORTS, "extra-high", "extra high") else low


def _model_ok(claimed: str, used: str) -> bool:
    a, b = claimed.lower().strip(), used.lower().strip()
    return a in b or b in a


def _run_id(v) -> int | None:
    m = re.fullmatch(r"\s*#?(\d+)\s*", str(v)) if isinstance(v, (int, str)) and not isinstance(v, bool) else None
    return int(m.group(1)) if m else None


def check_claims(db: DB, result, run_id: int | None) -> tuple:
    """(result, lines): a worker's result rows that name a `model` or `effort` given what the run used,
    read from its runs row. The hand-off's `metrics` speak for its own run (`run_id`) unless they name a
    `run`; a dict row of CLAIM_KEYS (a list, or a dict of rows such as `cells`) is checked only when it
    names its `run`. Each checked row gets `used` {model, effort} (the metrics: `metrics_used`); each mismatch a line, also kept in
    the result's `claim_mismatch`. A run with no record of the field is unknown, never a mismatch."""
    if not isinstance(result, dict):
        return result, []
    rows: list[tuple[str, dict, int | None]] = []
    if isinstance(result.get("metrics"), dict):
        rows.append(("metrics", result["metrics"], _run_id(result["metrics"].get("run", run_id))))
    for key in CLAIM_KEYS:
        got = result.get(key)
        items = got.items() if isinstance(got, dict) else enumerate(got) if isinstance(got, list) else ()
        for name, row in items:
            if isinstance(row, dict) and "run" in row:
                rows.append((f"{key}[{name!r}]" if isinstance(name, str) else f"{key}[{name}]", row, _run_id(row["run"])))
    rows = [(w, r, rid) for w, r, rid in rows if rid and any(isinstance(r.get(k), str) and r[k].strip()
                                                             for k in ("model", "effort"))]
    if not rows:
        return result, []
    have = {int(r["id"]): r for r in db.q(f"SELECT id, model, effort FROM runs WHERE id IN "
                                          f"({','.join('?' * len(rows))})", [rid for _, _, rid in rows])}
    lines = []
    result = dict(result)
    for where, row, rid in rows:
        run = have.get(rid)
        used = {"model": str(run["model"] or "") if run else "", "effort": str(run["effort"] or "") if run else ""}
        if where == "metrics":   # beside them: `metrics` stays name -> value for its readers (trend)
            result["metrics_used"] = used
        else:
            row["used"] = used
        bad = []
        if isinstance(row.get("effort"), str) and row["effort"].strip() and used["effort"] \
                and _norm_effort(row["effort"]) != _norm_effort(used["effort"]):
            bad.append(f"effort {row['effort']!r}, run #{rid} recorded {used['effort']!r}")
        if isinstance(row.get("model"), str) and row["model"].strip() and used["model"] \
                and not _model_ok(row["model"], used["model"]):
            bad.append(f"model {row['model']!r}, run #{rid} recorded {used['model']!r}")
        if bad:
            lines.append(f"result check: {where} claims " + " and ".join(bad)
                         + ": the row reports a run that did not use what it names; correct it or rerun")
    if lines:
        result["claim_mismatch"] = lines
    return result, lines
