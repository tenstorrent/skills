# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Project tools still enforcing a Restrictions item that was loosened or retired.

After the user's word loosens or removes a Restrictions item (a quoted edit, or a `replaces` of a
whole Restrictions section with looser wording), or `over` retires one, the project's own tools
(bin/, hooks/, schedules) may still carry the old rule. restriction_tools names them in a queued
`restriction_tools` event for the next digest; it never edits anything. coordinator.apply() calls it
once per turn, with the turn's tasks, so a tool a task of that batch already names is left out. An
unsourced (advisory) item dropped, by anyone and with any `over` or none, whose words the Brief or a sourced Restrictions item
still states (still_stated) is left out too: that rule still binds, so its tools are right.

only_blocks_lines gives the daily review, per Restrictions item, the 7-day count of blocked runs and
asks citing it against refused actions citing it, and lists an item that only blocked work: evidence
for the coordinator's one-time ask to loosen it. Only the user's own words loosen a rule.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .project import Project

# A term a tool would carry word for word: `code`, a path or dotted name, an UPPER_CASE name.
_TOOL_TERM_RE = re.compile(r"`([^`\n]{3,60})`|(?<![\w/.-])([A-Za-z0-9-]*[A-Za-z0-9][_/.][A-Za-z0-9_/.-]*[A-Za-z0-9])"
                           r"|\b([A-Z][A-Z0-9]*_[A-Z0-9_]+|[A-Z]{4,})\b")
_CODE_SPAN = re.compile(r"`([^`\n]*)`")
TOOL_SCAN_FILES = 2000   # files read per check at most (bin/ and hooks/ trees)
TOOL_SCAN_BYTES = 262144   # a larger file is not a tool script


def tool_term_ids(text: str) -> set[str]:
    """The identifiers (_TOOL_TERM_RE) of `text`. Backticks pair in order: a code span too long to be
    one term gives the terms inside it, and its closing backtick never opens a span with the next one
    (the text between two spans is prose, not a term)."""
    out: set[str] = set()

    def bare(seg: str) -> None:   # text with no paired backticks: paths, dotted and UPPER_CASE names only
        out.update(next(g for g in m.groups() if g) for m in _TOOL_TERM_RE.finditer(seg.replace("`", " ")))
    pos = 0
    for m in _CODE_SPAN.finditer(text):
        bare(text[pos:m.start()])
        if 3 <= len(m.group(1)) <= 60:
            out.add(m.group(1))
        else:
            bare(m.group(1))
        pos = m.end()
    bare(text[pos:])
    return out


def tool_terms(old: str, new: str, charter: str = "",
               lines: list[str] | tuple = ()) -> tuple[set[str], set[str], set[str]]:
    """The old item's distinctive terms for restriction_tools, as (ids, words, exact): identifiers
    (_TOOL_TERM_RE) of its changed sentences that neither the new text nor the current `charter` (any
    section) still has, and its rule words (_rule_words) the new text dropped. When the new text dropped
    fewer than two, or all of them (a removal or retirement), the words are all of the changed
    sentences' but only the distinctive ones (distinctive_words over the scanned tool `lines`), and
    `exact` holds the changed sentences' quoted phrases and user-message cites (exact_terms), which
    hit on their own; else `exact` is empty. A new text whose rule words are all citation words
    (CITE_STEMS: "(User's word, 2026-10-05.)") keeps no rule: a removal."""
    from .coordinator import _changed_sentences, _rule_words
    changed = " ".join(_changed_sentences(old, new)) or old
    ids = tool_term_ids(changed)
    ids = {t for t in ids if t not in new and t not in charter and not re.fullmatch(r"[\d./-]+", t)}
    every, kept = _rule_words(changed), _rule_words(new)
    words = every - kept
    if 2 <= len(words) < len(every) and not kept <= CITE_STEMS:
        return ids, words, set()
    return ids, distinctive_words(every, lines), exact_terms(changed, new + "\n" + charter)


# A stem on more than this share of the scanned tool lines is too common to match. An absolute share,
# not the N rarest stems: a rank keeps N stems of an item whose words are all common. Measured on a tool
# tree of about 20,000 lines, whose commonest stems were each under 3.5%: a 5% cut dropped nothing,
# 1% still flagged 33 files, 0.5% flagged 7 including every real citer, 0.2% lost the real citers.
COMMON_SHARE = 0.005
COMMON_MIN = 20       # ... and on more than this many lines: on a small tool tree 0.5% is a line or two,
                      # and the harness's own scripts alone name 'reset' or 'host' on ten
# Stems (_rule_words) of a citation rather than a rule: "(user msg #12)", "the user's word, October 5".
CITE_STEMS = frozenset({"user", "word", "msg", "messag", "january", "february", "march", "april", "jun", "july",
                        "august", "september", "october", "november", "december"})


def distinctive_words(words: set[str], lines: list[str] | tuple) -> set[str]:
    """The stems of `words` found (as word starts) on at most COMMON_SHARE of the non-blank `lines`,
    or on at most COMMON_MIN of them: 'work', 'project' or 'fail' of a long item match almost any line."""
    low = [x.lower() for x in lines if x.strip()]
    most = max(COMMON_MIN, COMMON_SHARE * len(low))
    return {w for w in words
            if sum(1 for x in low if re.search(rf"\b{re.escape(w)}", x)) <= most}


_QUOTED = re.compile(r"[\"“]([^\"”\n]{4,80})[\"”]|(?<!\w)['‘]([^'’\n]{4,80})['’](?!\w)")


def exact_terms(changed: str, kept: str = "") -> set[str]:
    """What a tool enforcing the item would carry word for word: its quoted phrases of two or more words
    (lowercased, spaces folded) that `kept` (the new text and charter) no longer has, and its user-message
    cites, as "#N" (restriction_source.CITE_RE)."""
    from .restriction_source import CITE_RE
    keep = " ".join(kept.lower().split())
    out = {p for m in _QUOTED.finditer(changed) if len((p := " ".join((m[1] or m[2]).lower().split())).split()) >= 2
           and p not in keep}
    return out | {"#" + re.search(r"\d+$", m.group())[0] for m in CITE_RE.finditer(changed)}


def enabled_schedules(text: str) -> str:
    """harness/schedules.json with each disabled entry (`"enabled": false`) blanked to spaces, so a
    disabled schedule names nothing and a hit's line number still points into the file. Text that is
    not a JSON list of entries is returned as it is."""
    dec, i = json.JSONDecoder(), text.find("[")
    if i < 0 or text[:i].strip():
        return text
    i += 1
    while True:
        i = re.compile(r"[\s,]*").match(text, i).end()
        if i >= len(text) or text[i] == "]":
            return text
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            return text
        if isinstance(obj, dict) and not obj.get("enabled", True):
            text = text[:i] + re.sub(r"[^\n]", " ", text[i:end]) + text[end:]
        i = end


def tool_hit(line: str, ids: set[str], words: set[str], exact: set[str] = frozenset(),
             rare: set[str] | None = None) -> list[str]:
    """What a line names of the old item: an identifier, a quoted phrase or user-message cite of
    `exact` (exact_terms), or else two or more of its rule words (as word starts), one of them in
    `rare` (distinctive_words) when given; [] when none."""
    from .restriction_source import CITE_RE
    got = sorted(t for t in ids if t in line)
    low = line.lower()
    if exact:
        folded = " ".join(low.split())
        cites = {"#" + re.search(r"\d+$", m.group())[0] for m in CITE_RE.finditer(line)}
        got += sorted(t for t in exact if (t in cites if t.startswith("#") else t in folded))
    if got:
        return got
    got = sorted(w for w in words if re.search(rf"\b{re.escape(w)}", low))
    return got if len(got) >= 2 and (rare is None or rare & set(got)) else []


def still_stated(charter: str, old: str) -> str:
    """The binding charter section that still states every item of retired advisory text `old` (one
    Restrictions item or a replaced section's items): "Brief" when one paragraph or bullet of a `## Brief`
    section, else "Restrictions" when one sourced Restrictions item (restriction_source.sourced), holds
    all of the item's identifiers (_TOOL_TERM_RE) and at least two of its rule words, and at least two
    thirds of them. "" when an item is stated nowhere, or `old` names nothing to compare."""
    from .coordinator import _rule_words
    from .prompts import charter_restrictions, charter_sections
    from .restriction_source import sourced, split_items
    units: list[tuple[str, str]] = []
    for head, body in charter_sections(charter):
        if " ".join(head[3:].split()).lower().startswith("brief"):
            units += [("Brief", " ".join(" ".join(it).split())) for it in split_items("\n".join(body))]
    units += [("Restrictions", t) for t in (" ".join(" ".join(it).split())
                                            for it in split_items(charter_restrictions(charter))) if sourced(t)]
    found = []
    for item in (" ".join(" ".join(it).split()) for it in split_items(old)):
        if item.startswith("#"):
            continue
        ids = tool_term_ids(item)
        words = _rule_words(item)
        if len(words) < 2:
            return ""
        hit = next((name for name, unit in units if all(t in unit for t in ids)
                    and 3 * len(words & _rule_words(unit)) >= 2 * len(words)), "")
        if not hit:
            return ""
        found.append(hit)
    return min(found, default="")   # "Brief" before "Restrictions" when items differ


def replaced_items(body: str, text: str) -> list[tuple[str, str]]:
    """The items of a Restrictions section a `replaces` retired that its new `text` loosens or drops,
    as (old item, the new item it became or ""). An item equal to a new item, compared loosely, is
    left out; one only contained in a new item (extended with an exception) is not. Any other item
    is paired with the new item that shares the most of its rule words (_widens), and judged as a
    quoted edit to it (_loosens); an item with no such new item was dropped."""
    from .coordinator import _items, _loose, _loosens, _rule_words, _widens
    olds = [" ".join(body[s:e].split()) for s, e, _ in _items(body)]
    news = [" ".join(text[s:e].split()) for s, e, _ in _items(text)]
    kept = {_loose(n) for n in news}
    out = []
    for old in olds:
        if not old or _loose(old) in kept:
            continue
        near = [n for n in news if _widens(old, _rule_words(n))]
        new = max(near, key=lambda n: len(_rule_words(old) & _rule_words(n)), default="")
        if _loosens(old, new):
            out.append((old, new))
    return out


def restriction_tools(p: Project, loosened: list[tuple], batch: list[int]) -> list[str]:
    """After Restrictions items were loosened or removed, name the project's own tools that still carry
    the old wording: files under bin/ and hooks/ (project root and harness), harness/schedules.json,
    and the command and heal check of each enabled schedule, whose lines name an identifier of the old item
    or two of its dropped rule words (tool_terms; an identifier the current charter still states anywhere
    is not one; a removed or retired item matches only its distinctive words, quoted phrases and cites;
    two words hit only with one distinctive word among them),
    each named with the terms it matched. A disabled schedule, in the table or its schedules.json entry, is left out. `loosened`: (old item, new text, over[,
    advisory]), `over` empty for the user's word, else the end that retired the item; `advisory`: the dropped
    items were all unsourced (restriction_source.drops_unsourced), so whoever dropped them and with any `over`,
    one the Brief or a sourced item still states is left out, as is `over` "unsourced". A file a task of this batch
    names (its title or spec has the file's path or name) is left out. One queued restriction_tools
    event per item with such files, for the next digest: model-free, it edits nothing. Returns the
    event texts."""
    from .coordinator import clip
    files: list[tuple[str, str]] = []   # (label, text)
    seen = 0
    for base in (p.root, p.harness):
        for sub in ("bin", "hooks"):
            top = base / sub
            if not top.is_dir():
                continue
            for f in sorted(top.rglob("*")):
                if seen >= TOOL_SCAN_FILES:
                    break
                if not f.is_file() or any(x in f.parts for x in (".git", "__pycache__", "node_modules")):
                    continue
                seen += 1
                try:
                    if f.stat().st_size > TOOL_SCAN_BYTES:
                        continue
                    raw = f.read_bytes()
                except OSError:
                    continue
                if b"\0" not in raw:
                    files.append((str(f.relative_to(p.root)), raw.decode("utf-8", "replace")))
    sched = p.harness / "schedules.json"
    if sched.is_file() and sched.stat().st_size <= TOOL_SCAN_BYTES:
        files.append((str(sched.relative_to(p.root)), enabled_schedules(sched.read_text(errors="replace"))))
    for r in p.db.q("SELECT name, payload FROM schedules WHERE enabled=1"):
        payload = json.loads(r["payload"] or "{}")
        heal_block = payload.get("heal") or {}
        cmds = [payload.get("command")] + [heal_block.get(k) for k in ("check", "fix", "suppress")]
        files.append((f"schedule {r['name']}", "\n".join(str(c) for c in cmds if c)))
    named = " ".join(f"{t['title']} {t['spec']}" for t in (p.db.task(i) for i in batch) if t)
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    out = []
    for old, new, over, *advisory in loosened:
        if (over == "unsourced" or any(advisory)) and still_stated(charter, old):
            continue   # a dropped advisory copy of a rule the Brief or a sourced item still binds: tools stay right
        lines = [x for _, t in files for x in t.splitlines()]
        ids, words, exact = tool_terms(old, new, charter, lines)
        rare = distinctive_words(words, lines)
        hits = []
        for label, text in files:
            line, got = next(((n, g) for n, x in enumerate(text.splitlines(), 1)
                              if (g := tool_hit(x, ids, words, exact, rare))),
                             (0, []))
            if not line or label in named or (not label.startswith("schedule ") and Path(label).name in named):
                continue
            hits.append(label + (f":{line}" if not label.startswith("schedule ") else "")
                        + f" ({', '.join(clip(g, 40) for g in got[:4])})")
        if not hits:
            continue
        x = ((f"`over` retired the Restrictions item \"{clip(old, 200)}\" ({clip(over, 150)})" if over
              else f"The user's word loosened the Restrictions item \"{clip(old, 200)}\"")
             + f", but these project tools still name its terms and no task of that batch names them: "
             + ", ".join(hits[:10])
             + (f" and {len(hits) - 10} more" if len(hits) > 10 else "")
             + ". Queue a priority-1 task updating every tool that enforces the old wording, or say in "
               "`summary` why these do not enforce it.")
        out.append(x)
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
               (time.time(), "harness", "restriction_tools", "normal", x, "queued"))
    return out


# The daily review's 7-day count per Restrictions item: evidence for the coordinator's one-time ask
# to drop or loosen a rule that only blocks work. Model-free; it never edits the charter or asks.
CITE_WINDOW_S = 7 * 86400
BLOCKS_MIN = 3   # review.rule_blocks_min: blocked-work citations that list an item


# A text that names a restriction as its reason ("the Restrictions", "the charter forbids", "not allowed by").
_REASON_RE = re.compile(r"\brestrictions?\b|\b(?:charter|rule)s?\s+(?:forbid|ban|prohibit|bar)\w*|"
                        r"\b(?:not allowed|forbidden|banned|prohibited)\s+by\b", re.I)


def _ban_acts(sentence: str) -> set[str]:
    """A ban sentence's own action words, stemmed: the actions (coordinator._ACTIONS) after its ban
    word outside condition clauses ("never reboot X while its queue runs": reboot), else those
    anywhere outside them ("push only to X": push), else the first rule word after the ban word."""
    from .coordinator import _ACTIONS, _BAN_RE, _CONDITION_RE, _rule_words, _stem
    m = _BAN_RE.search(sentence)
    if not m:
        return set()

    def acts(text: str) -> list[str]:
        ws = re.findall(r"[a-z][a-z0-9-]*[a-z0-9]", _CONDITION_RE.sub(" ", text.lower()))
        return [_stem(w) for w in ws if _ACTIONS.get(_stem(w)) or _ACTIONS.get(w)]
    after = sentence[m.end():]
    words = [w for w in re.findall(r"[a-z][a-z0-9-]*[a-z0-9]", after.lower()) if _rule_words(w)]
    found = acts(after) or acts(sentence) or [_stem(w) for w in words[:1]]
    return set(found)


def _cites(item: str, bans: list[tuple[str, set[str]]], text: str) -> bool:
    """Whether a blocked run's, ask's or refusal's text is about a Restrictions item's ban: it quotes
    the item or one of its ban sentences, or it shares rule words with a ban sentence (_widens) and
    has that ban's own action word (_ban_acts) or names a restriction as its reason (_REASON_RE).
    Duty sentences ("Always fetch and rebase ...") and shared object words alone never cite."""
    from .coordinator import _loose, _rule_words, _widens
    low = _loose(text)
    if any(_loose(x) in low for x in [item] + [s for s, _ in bans]):
        return True
    words = _rule_words(text)
    reason = bool(_REASON_RE.search(text))
    return any(_widens(s, words) and (acts & words or reason) for s, acts in bans)


def citations(p: Project, now: float | None = None, window_s: float = CITE_WINDOW_S) -> list[dict]:
    """Per item of the charter's Restrictions sections, in charter order: {item, blocked, asks,
    stopped, tasks, ask_ids} over the last `window_s`. `blocked`: task_blocked events (a run that
    ended blocked, or a task the daemon blocked) citing the item, their task ids in `tasks`; `asks`:
    asks sent to the user citing it, their message ids in `ask_ids`; `stopped`: coordinator actions
    the guard or relay refused (rejected_actions; the ask gate's ask_user refusals are left out)
    citing it. A text cites an item when it is about one of the item's bans (_cites); one text may
    cite several items."""
    from .coordinator import _BAN_RE, _items, _sentences
    from .prompts import charter_restrictions
    try:
        body = charter_restrictions(p.charter_path.read_text())
    except OSError:
        return []
    items = [" ".join(body[s:e].split()) for s, e, _ in _items(body)]
    rows = [{"item": x, "bans": [(s, _ban_acts(s)) for s in _sentences([x]) if _BAN_RE.search(s)],
             "blocked": 0, "asks": 0, "stopped": 0, "tasks": [], "ask_ids": []} for x in items if x]
    if not rows:
        return []
    since = (time.time() if now is None else now) - window_s
    texts: list[tuple[str, str, int | None]] = []
    for e in p.db.q("SELECT text, task FROM events WHERE kind='task_blocked' AND ts>=?", (since,)):
        tid = e["task"] or (int(m[1]) if (m := re.match(r"#(\d+)\b", e["text"] or "")) else None)
        texts.append(("blocked", e["text"], tid))
    texts += [("asks", m["text"], m["id"]) for m in p.db.q(
        "SELECT id, text FROM messages WHERE direction='out' AND kind='ask' AND ts>=?", (since,))]
    for e in p.db.q("SELECT text, data FROM events WHERE kind='rejected_actions' AND ts>=?", (since,)):
        try:
            got = json.loads(e["data"]) if e["data"] else None
        except ValueError:
            got = None
        texts += [("stopped", str(x), None) for x in (got if isinstance(got, list) else [e["text"]])
                  if not str(x).startswith("ask_user:")]
    for key, text, ref in texts:
        for r in rows:
            if _cites(r["item"], r["bans"], text or ""):
                r[key] += 1
                ids = r["tasks"] if key == "blocked" else r["ask_ids"] if key == "asks" else None
                if ids is not None and ref is not None and ref not in ids:
                    ids.append(ref)
    return [{k: v for k, v in r.items() if k != "bans"} for r in rows]


def only_blocks_lines(p: Project, now: float | None = None, least: int = BLOCKS_MIN) -> list[str]:
    """The self-efficiency audit's lines: one per Restrictions item whose blocked runs and asks
    reached `least` in 7 days while it stopped no banned action; [] when none qualify."""
    from .coordinator import clip
    out = []
    for r in citations(p, now):
        held = r["blocked"] + r["asks"]
        if held >= least and not r["stopped"]:
            out.append(f"Restrictions item only blocked work in {CITE_WINDOW_S // 86400} d (held {r['blocked']} "
                       f"blocked run{'s' * (r['blocked'] != 1)} and {r['asks']} ask{'s' * (r['asks'] != 1)}, "
                       f"stopped no banned action): \"{clip(r['item'], 160)}\"" + _ids(r))
    return out


def _ids(r: dict) -> str:
    """The cited task and ask ids, for the coordinator to check before it asks."""
    got = ([f"tasks {', '.join(f'#{t}' for t in r['tasks'][:10])}"] if r["tasks"] else []) + (
        [f"asks {', '.join(str(a) for a in r['ask_ids'][:10])}"] if r["ask_ids"] else [])
    return f" (cited by {' and '.join(got)})" if got else ""
