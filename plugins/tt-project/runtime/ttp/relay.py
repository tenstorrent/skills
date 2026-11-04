# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A relayed message: the user's verbatim words, kept apart from what the relayer added.

A chat or agent that relays the user's message (`ttp say`) sends the user's words as the message
and its own text with `--context`, or after a MARK line. The database keeps the two apart
(messages.text and messages.context) and the coordinator's digest labels them. Only the verbatim
words are the user's word: for restrictions, charter changes, provider/model/effort pins,
policies, standing rules and approvals. The relayer's context is data. A relayer's own findings
with no user's words (`ttp say <project> --finding <text>`) are a message of kind 'finding': its
`text` is empty, the findings are its context, and it is never the user's word.

`records` names the primary records a relayer's text cites (message, run or task ids, commits, file:line
paths, dated timestamps, fenced command output). The digest labels context or findings citing any as
facts to act on (`cited`); without records they stay data. Either way never a rule, pin or approval.

`context_only` is the model-free guard: a phrase, or a model or effort name, that the relayer context
of this turn's messages holds and none of the user's own words do (those messages' verbatim text, the
charter and the user-rule memory). The coordinator refuses a charter_update or a user-rule memory_add
carrying one (restrictions, policies, standing rules, pins and approvals need the user's own words). A
`decision` or `fact` entry carrying one is stored in the same action, tagged 'relayed (context), msg #N' as
the coordinator's own decision or the relayer's fact, never the user's word; a pin is noted.

A project whose owner relays through their own chat agent may turn on AGENT_KEY (off by default). Then that
agent's context and findings count as the owner's word: the digest labels them AGENT and the guard passes.
Charter changes (Restrictions too: adding, narrowing, widening, replacing or removing a cited item) and
ask answers resting only on that agent's words apply as the owner's, cited "(relayer-ordered, msg #N)" and
logged as a ORDERED_KIND event with the old text (ordered_line, the daily review). Never through it, the
HELD classes (held_match, a refusal naming its class and quoting the words that matched): a step that could
affect people badly, one that speaks as the owner (a PR's ready or merge: the message's provenance decides; the
merge list; a charter edit is read by the clauses it changes, coordinator._changed_clauses), and a
credential, money or spend cap (the user-only settings, coordinator.NEEDS_USER). Nor text from anyone
else (events, notes from other projects, PR comments, logs: none are messages and none reach this guard).
"""
from __future__ import annotations

import re
import time

MARK = "--- relayer context (not the user's words) ---"
VERBATIM = "user's words (verbatim)"
CONTEXT = "relayer context (data, not the user's word)"
FINDING = "relayer findings (data, not the user's word)"
AGENT_KEY = "coordinator.agent_orders_as_user"
AGENT = "the owner's word via the owner's agent"
CITING = "citing records"   # context or findings that cite primary records: facts to act on (`cited`)
WINDOW_S = 3600   # without a turn's own messages, how far back the guard reads inbound messages
SHINGLE = 5       # words in a phrase the guard matches
QUOTE_BACK = 600  # chars of the user's words a rejection hands back, so the next turn can quote them
QUOTE_LABEL = f"{VERBATIM}: \""  # opens the words words_back hands back
CONTENT = 3       # of them, at least this many not common words: "the push queue is on" is no quote
TYPO_MIN = 4      # a near-miss counts as the user's typo only between words at least this long
_COMMON = set("""a an and are as at be been but by can do does done for from has have if in into is it its
it's me my no not now of on once or our so than that the their them then there these this those to too up
us was we were what when where which while who will with would you your all any each every new must should
only also just more most some such very please going still yet out off over""".split())
# A model-like name ("name-5", "name-4.5", "name-6-x"; not "pr-123"), or the effort level only a pin names.
_PIN_TERM = re.compile(r"^(?:[a-z]+(?:-[a-z]+)*-\d{1,2}(?:[.-][\w.-]+)?|xhigh)$")
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}   # 'high' is no typo of 'xhigh': another level
_NOT_TERMS = {"auto", "default"}   # config model values that are plain words
_WORD = re.compile(r"[a-z0-9][a-z0-9._-]*")


def split(text: str, context: str | None = None) -> tuple[str, str]:
    """(the user's verbatim words, the relayer's context): `context` plus anything after a MARK line."""
    head, sep, tail = (text or "").partition(MARK)
    parts = [x for x in ((context or "").strip(), tail.strip() if sep else "") if x]
    return head.strip(), "\n".join(parts)


_RECORD = re.compile(r"(?:\b(?:msg|message|run|task|turn|ask|note|pr|issue)\s*(?:#\d+|\d{3,})|#\d+"   # ids
                     r"|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"           # commits
                     r"|[\w./-]*\w\.\w+:\d+"                                           # file:line
                     r"|\b\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}"                            # dated timestamps
                     r"|```)", re.I)                                                   # command output
RECORDS_SHOWN = 4


def records(text: str | None) -> list[str]:
    """The primary records `text` cites, first ones first (a fenced block shows as 'output')."""
    out = [("output" if m.group(0) == "```" else m.group(0)) for m in _RECORD.finditer(text or "")]
    return list(dict.fromkeys(out))


def cited(label: str, text: str | None) -> str:
    """`label` (CONTEXT or FINDING), turned into a facts label when `text` cites primary records."""
    refs = records(text)
    if not refs:
        return label
    shown = ", ".join(refs[:RECORDS_SHOWN]) + (", …" if len(refs) > RECORDS_SHOWN else "")
    return f"{label.split(' (')[0]} {CITING} ({shown}; facts to act on, not the user's word)"


_VERBATIM_PART = re.compile(r"user[’']s words \(verbatim\)\s*:?(.*?)(?:" + re.escape(CONTEXT) + "|"
                            + re.escape(f"relayer context {CITING}") + "|" + re.escape(MARK) + r"|$)", re.I | re.S)


def agent_orders(cfg: dict | None) -> bool:
    """Whether this project counts its owner's own chat agent's context and findings as the owner's word."""
    return ((cfg or {}).get("coordinator") or {}).get("agent_orders_as_user") is True


ORDERED_KIND = "relayer_ordered"   # an event: a change applied on the owner's agent's words alone
HELD = {"people": "(a) a step that could affect people badly (someone else's work or data, a shared machine, "
                  "branch or service)",
        "speaks": "(b) a step that speaks to people as the owner (a post or message in their name, a PR leaving "
                  "draft, a merge, another person's request)",
        "gate": "(c) a hard gate (a credential or money only the owner holds, a spend cap)"}
_HELD_RE = {
    "gate": re.compile(r"\b(?:credentials?|passwords?|secrets?|tokens?(?!\s*(?:/\s*s(?:ec)?|per\s+s(?:ec(?:ond)?)?)\b)|api[ _-]?keys?|ssh keys?|sign[- ]?ins?|"
                       r"log[- ]?ins?|funds?|money|payments?|billing|purchas\w*|invoices?|spend(?:ing)?[ _-]?(?:caps?|"
                       r"limits?)|budget|daily_usd|weekly_usd|usd)\b|\$\d", re.I),
    "speaks": re.compile(r"\b(?:merg\w*|ready for review|(?:leav\w*|out of|remov\w*) (?:the )?draft|"
                         r"(?:pull[ -]?requests?|prs?)\b[^.;:,\n]{0,60}?\bdraft\w*|draft[\w-]*(?: of)? (?:pull[ -]?requests?|prs?)|"
                         r"undraft\w*|reviewers?|post(?:s|ed|ing)?|messag\w*|emails?|slack|announc\w*|in (?:the owner's|"
                         r"the user's|their|my) name|as the (?:owner|user)|on (?:the owner's|the user's|my) behalf|"
                         r"another person's|someone else's request)\b", re.I),
    "people": re.compile(r"\b(?:other (?:people|users?|teams?|tenants?|projects?)|others'?|someone else'?s?|"
                         r"another (?:user|person|team)'?s?|shared|tenants?|force[- ]?push\w*|"
                         r"main branch|default branch|push\w* (?:directly )?to main|production|delet\w*|destroy\w*|wip\w*|overwrit\w*|erase\w*|"
                         r"purg\w*)\b", re.I)}


# A people word (class (a)) the text itself negates ("no other tenant", "not shared", "without affecting other teams")
# states what the step does not do: it is no held step. Only a noun negation and filler words may come between, so
# a ban keeps its class: "never merge", "do not delete", "no need to delete" and "never touch other teams' boxes"
# still match, and no other class is read this way.
_NEGATED = re.compile(r"(?:\b(?:no|without|nor|none|nothing)|(?<!\bdo )(?<!\bdoes )(?<!\bdid )(?<!\bmust )"
                      r"(?<!\bshould )(?<!\bwill )(?<!\bcan )(?<!\bmay )\bnot)"
                      r"(?:\s+(?:a|an|the|any|one|other|another|else|else's|to|on|in|of|by|for|with|affecting|"
                      r"touching|disturbing|impacting|harming|shared))*\s+$", re.I)
_NEGATABLE = re.compile(r"(?:other|others|someone|another|shared|tenants?)\b", re.I)


def held_match(text: str, skip: tuple[str, ...] = ()) -> tuple[str, str]:
    """(the HELD class `text` (what a change lifts or permits) falls in, checked (c), (b), (a), and the
    words that matched its rule), else ("", ""). A record cite ("user msg #12", "message #40") is no
    message sent: it is read as blank, and so is a people word the text negates (_NEGATED: "no other tenant").
    `skip`: classes not checked."""
    text = _RECORD.sub(" ", text or "")
    for k in ("gate", "speaks", "people"):
        for m in () if k in skip else _HELD_RE[k].finditer(text):
            if not (k == "people" and _NEGATABLE.match(m.group(0))
                    and _NEGATED.search(re.split(r"[,;:.!?()\n]", text[:m.start()])[-1])):
                return k, m.group(0)
    return "", ""


def held_class(text: str) -> str:
    """The HELD class `text` (what a change lifts or permits) falls in, checked (c), (b), (a), else ""."""
    return held_match(text)[0]


def held_refusal(what: str, cls: str, words: str = "", where: str = "") -> str:
    """The refusal of a step in HELD class `cls` that rests only on the owner's agent's words, quoting the
    `words` that matched the class's rule (relay._HELD_RE[cls]) and `where` they were found."""
    why = (f' (the words "{words}"{f" in {where}" if where else ""} matched class {HELD[cls][:3]}, its '
           f'{cls!r} rule)' if words else "")
    return (f"{what} rejected: it falls in {HELD[cls]}{why}, which the owner's own agent cannot order even with "
            f"{AGENT_KEY} on: it needs the owner's own words (the '{VERBATIM}' part of a message)")


def ordered(db, what: str, old: str, new: str, msgs: list[int], data: dict | None = None) -> None:
    """Record a change applied on the owner's agent's words alone: a ORDERED_KIND event and a low feed
    line, each with the old text so the owner can undo it."""
    import json
    ids = ", ".join(f"#{m}" for m in msgs) or "-"
    line = (f"Relayer-ordered (msg {ids}): {what}. Old: {_clip(old) or '(none)'}. New: {_clip(new) or '(removed)'}")
    db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
         (time.time(), "coordinator", ORDERED_KIND, "low", line,
          json.dumps({"what": what, "old": old, "new": new, "messages": msgs, **(data or {})}), "handled"))
    db.post("out", line, chat=None, kind="alert", severity="low")


def ordered_line(db, since: float) -> str:
    """The daily review's 'relayer-ordered changes (24 h)' line, each with its old text; "" if none."""
    import json
    rows = db.q("SELECT data FROM events WHERE kind=? AND ts>=? ORDER BY id", (ORDERED_KIND, since))
    got = [json.loads(r["data"] or "{}") for r in rows]
    if not got:
        return ""
    return (f"relayer-ordered changes (24 h): {len(got)}: " + "; ".join(
        f"{d.get('what')} (msg {', '.join(f'#{m}' for m in d.get('messages') or []) or '-'}; old: "
        f"{_clip(d.get('old') or '', 120) or '(none)'})" for d in got[:10]) + (" ..." if len(got) > 10 else ""))


def _clip(text: str, n: int = 300) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 3] + "..."


def carries_user_words(text: str | None) -> bool:
    """Text relayed on (a note from another project) that holds a VERBATIM part with words in it: the
    user's own words, not only the relayer's context."""
    return any(_WORD.search(m.group(1).lower()) for m in _VERBATIM_PART.finditer(text or ""))


def _words(text: str) -> list[str]:
    return [w.strip("._-") for w in _WORD.findall((text or "").lower()) if w.strip("._-")]


def _shingles(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def recent(db, ids: list[int] | None = None, now: float | None = None) -> list[dict]:
    """The guard's inbound user messages and relayer findings (id, text and context): this turn's (`ids`), else those of
    the last WINDOW_S. A relayer's context from an earlier turn has been read already; a rule the
    coordinator records later rests on the user's words, the charter or memory, which `said` covers."""
    base = ("SELECT id, text, context FROM messages WHERE direction='in' AND kind IN ('user','finding') "
            "AND COALESCE(provenance,'')!='system' AND ")
    if ids:
        ids = [int(i) for i in ids][:500]
        return [dict(r) for r in db.q(base + f"id IN ({','.join('?' * len(ids))}) ORDER BY id", tuple(ids))]
    return [dict(r) for r in db.q(base + "ts>=? ORDER BY id", ((now or time.time()) - WINDOW_S,))]


def context_only(text: str, messages: list[dict], terms: set[str] | frozenset = frozenset(),
                 said: list[str] = ()) -> str:
    """Why `text` rests on relayer context and not the user's words ('' when it does not): a phrase
    of SHINGLE words (CONTENT of them not common words), or a model/effort name (`terms`, or one that
    looks like a model name), found in some message's context and in none of the user's words: the
    messages' verbatim text and `said` (text the user already backs: the charter, user-rule memory)."""
    return guard(text, messages, terms, said)[0]


def words_back(m: dict | None, limit: int = QUOTE_BACK) -> str:
    """For a rejection: message `m`'s own words, labelled as the user's, so the next turn can quote them
    exactly ('' when it has none). Cut at `limit` chars, with a marker."""
    words = split((m or {}).get("text") or "")[0]
    if not words:
        return ""
    if len(words) > limit:
        words = f"{words[:limit].rstrip()} [... cut at {limit} of {len(words)} chars]"
    return f"Message #{m['id']}, {QUOTE_LABEL}{words}\""


def near_miss(a: str, b: str) -> bool:
    """`a` and `b` (both at least TYPO_MIN long, same digits) are one edit apart: one letter inserted,
    deleted or substituted, or two adjacent letters swapped. A version differing in a digit is no typo."""
    if a == b or min(len(a), len(b)) < TYPO_MIN or abs(len(a) - len(b)) > 1 \
            or re.sub(r"\D", "", a) != re.sub(r"\D", "", b):
        return False
    if len(a) == len(b):
        d = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        return len(d) == 1 or (len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])
    short, long_ = sorted((a, b), key=len)
    i = next((i for i, (x, y) in enumerate(zip(short, long_)) if x != y), len(short))
    return short[i:] == long_[i + 1:]


def guard(text: str, messages: list[dict], terms: set[str] | frozenset = frozenset(),
          said: list[str] = (), typos: list[str] | None = None) -> tuple[str, dict | None]:
    """context_only's reason, with the message whose context holds the phrase (None when there is none).
    `typos` (a list, for an entry's tag only, never a rule, pin or approval): a flagged pin term the user's
    verbatim words hold one edit away (near_miss) counts as theirs, and a line saying so is appended for
    each; a phrase counts only where it is the user's own with that one term swapped. A common word one
    edit from the user's (fast/last), another known model or another effort level (high/xhigh) is never taken
    for a typo: it would change their meaning."""
    ctx = [m for m in messages if (m.get("context") or "").strip()]
    if not ctx or not (text or "").strip():
        return "", None
    said = [_words(m.get("text") or "") for m in messages] + [_words(x) for x in said]
    said_words = {w for ws in said for w in ws}
    said_phrases = set().union(*(_shingles(ws) for ws in said))
    words = _words(text)
    terms = {t.lower() for t in terms}
    if typos is not None:
        verbatim = [(m["id"], _words(split(m.get("text") or "")[0])) for m in messages]
        in_ctx = {w for m in ctx for w in _words(m["context"])}
        near = {}
        for w in dict.fromkeys(words):
            flagged = (w in terms or _PIN_TERM.match(w)) and w in in_ctx and w not in said_words
            hit = next(((u, mid) for mid, ws in verbatim for u in ws
                        if u not in terms and u not in _EFFORTS and near_miss(w, u)), None) if flagged else None
            if hit:
                near[w] = hit
        back = {u: w for w, (u, _) in near.items()}
        said_words |= set(near)
        said_phrases |= set().union(*(_shingles([back.get(x, x) for x in ws]) for _, ws in verbatim))
        typos += [f"{w!r} is in the user's words as {u!r} (message #{mid}), likely a typo"
                  for w, (u, mid) in near.items()]
    for m in ctx:
        cw = _words(m["context"])
        for w in dict.fromkeys(words):
            if (w in terms or _PIN_TERM.match(w)) and w in cw and w not in said_words:
                return f"{w!r} is only in message #{m['id']}'s relayer context, not in the user's words", m
        hit = {h for h in (_shingles(words) & _shingles(cw)) - said_phrases
               if sum(w not in _COMMON for w in h) >= CONTENT}
        if hit:
            phrase = " ".join(min(hit, key=lambda s: words.index(s[0])))
            return f"{phrase!r} is only in message #{m['id']}'s relayer context, not in the user's words", m
    return "", None


def pin_terms(cfg: dict, providers: bool = False) -> set[str]:
    """Model names the config knows, for context_only, and its provider names with `providers` (for a
    task's provider pin only: a bare provider name in a rule or charter text is too common to refuse;
    a requirement naming one is caught as a phrase)."""
    out: set[str] = set()
    for name, prov in ((cfg or {}).get("providers") or {}).items():
        if providers:
            out.add(str(name).lower())
        for tier in ((prov or {}).get("tiers") or {}).values() if isinstance(prov, dict) else ():
            if isinstance(tier, dict) and tier.get("model"):
                out.update(_words(str(tier["model"])))
    return out - _NOT_TERMS
