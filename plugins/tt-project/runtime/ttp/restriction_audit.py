# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Project tools still enforcing a Restrictions item that was loosened or retired.

After the user's word loosens or removes a Restrictions item (a quoted edit, or a `replaces` of a
whole Restrictions section with looser wording), or `over` retires one, the project's own tools
(bin/, hooks/, schedules, task probes) may still carry the old rule. A purely additive item (a scoped
`excepts` exception, a `both_hold` text) leaves every older item's text as it was: no check. Only the
changed clause's words match, as whole words. restriction_tools queues one priority-1 task per
change to update them, names them in a queued `restriction_tools` event for the next digest, and
digest_lines shows that task while it is open; it never edits a tool. coordinator.apply() calls it
once per turn, with the turn's tasks, so a tool a task of that batch already names is left out. An
unsourced (advisory) item dropped, by anyone and with any `over` or none, whose words the Brief or a sourced Restrictions item
still states (still_stated) is left out too: that rule still binds, so its tools are right.

only_blocks_lines gives the daily review, per Restrictions item, the 7-day count of blocked runs and
asks citing it against refused actions citing it, and lists an item that only blocked work: evidence
for the coordinator's one-time ask to loosen it. Only the user's own words loosen a rule.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from .project import Project

# A term a tool would carry word for word: `code`, a path or dotted name, an UPPER_CASE name.
_TOOL_TERM_RE = re.compile(r"`([^`\n]{3,60})`|(?<![\w/.-])([A-Za-z0-9-]*[A-Za-z0-9][_/.][A-Za-z0-9_/.-]*[A-Za-z0-9])"
                           r"|\b([A-Z][A-Z0-9]*_[A-Z0-9_]+|[A-Z]{4,})\b")
_CODE_SPAN = re.compile(r"`([^`\n]*)`")
TOOL_SCAN_FILES = 2000   # files read per check at most (bin/ and hooks/ trees)
TOOL_SCAN_BYTES = 262144   # a larger file is not a tool script
CHANGED_RATIO = 0.6   # word-for-word likeness at which a left-over old item is changed, not removed


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


def changed_clause(old: str, new: str) -> str:
    """The changed clause of a Restrictions item: a word diff of `old` against `new` (case and punctuation
    aside), and the clauses of `old` (ended by . ; : , ! or ?) whose words it drops or replaces, or that
    an insertion extends (tools enforce the old wording: the words `new` adds are not in it); all of `old`
    when `new` is empty. A clause the
    diff leaves alone is never in it ("Never X; never Y" -> "Never X; Y on weekends": Y's clause only),
    nor is any other item."""
    if not new.strip():
        return old
    toks, clause, c = old.split(), [], 0
    for t in toks:
        clause.append(c)
        c += bool(re.search(r"[.;:,!?][)\"'’”]*$", t))
    key = lambda ts: [re.sub(r"[^\w#]", "", t.lower()) for t in ts]
    added = new.split()
    touched = set()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=key(toks), b=key(added), autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            touched.update(clause[i1:i2])
        elif op == "insert" and toks:
            touched.add(clause[max(i1 - 1, 0)])
    return " ".join(t for t, k in zip(toks, clause) if k in touched)


def clause_words(text: str) -> set[str]:
    """The whole words a rule is about, lowercased: \\w+ words of four or more characters starting with a
    letter, and hyphenated names (_HYPHENATED: a machine alias such as box-a or tt-box-3, whose parts are
    too short to count), rule and filler words (coordinator._RULE_STOP) and citation words (CITE_WORDS)
    left out. Never stemmed: a tool line matches one only as a whole word of its own."""
    from .coordinator import _RULE_STOP
    low = text.lower()
    return {w for w in _TOKEN.findall(low) + _HYPHENATED.findall(low)
            if len(w) >= 4 and w[0].isalpha() or "-" in w and re.search(r"[a-z]", w)
            if w not in _RULE_STOP and w not in CITE_WORDS}


def tool_terms(old: str, new: str, charter: str = "",
               lines: list[str] | tuple = ()) -> tuple[set[str], set[str], set[str]]:
    """The changed item's terms for restriction_tools, as (ids, words, exact), all from its changed clause
    (changed_clause: the word diff of `old` against `new`), never from words both keep or from other
    items: identifiers (_TOOL_TERM_RE) that neither the new text nor the current `charter` (any section)
    still has, whole words (clause_words), and quoted phrases `new` and `charter` no longer have and
    user-message cites `new` no longer has (exact_terms). Words of the harness's own vocabulary (HARNESS_WORDS)
    are never terms; values with a unit (value_terms) are. A removal (no `new`, or a `new` of citation words
    only: "(User's word, 2026-10-05.)") keeps only its distinctive words: none the current `charter` still
    states (machine and resource names aside: _name), none common on the scanned tool `lines`
    (distinctive_words)."""
    changed = changed_clause(old, new)
    if not clause_words(new):
        changed, new = old, ""
    ids = tool_term_ids(changed)
    ids = {t for t in ids if t not in new and t not in charter and not re.fullmatch(r"[\d./-]+", t)}
    words = {w for w in clause_words(changed) if w not in HARNESS_WORDS} | value_terms(changed, new + "\n" + charter)
    if not new:   # a word another charter item still states names that item's tools too (names aside)
        low = charter.lower()
        words = distinctive_words({w for w in words if _name(w) or not _whole(w, low)}, lines)
    # A cite the new text keeps names the item as it now stands: no sign of its old wording. Only "#N" cites:
    # exact_terms already drops a phrase `new` keeps, and a phrase is user text, never a regex.
    exact = {t for t in exact_terms(changed, new + "\n" + charter)
             if not (t.startswith("#") and re.search(rf"{re.escape(t)}(?!\d)", new))}
    return ids, words, exact


# A stem on more than this share of the scanned tool lines is too common to match. An absolute share,
# not the N rarest stems: a rank keeps N stems of an item whose words are all common. Measured on a tool
# tree of about 20,000 lines, whose commonest stems were each under 3.5%: a 5% cut dropped nothing,
# 1% still flagged 33 files, 0.5% flagged 7 including every real citer, 0.2% lost the real citers.
COMMON_SHARE = 0.005
COMMON_MIN = 20       # ... and on more than this many lines: on a small tool tree 0.5% is a line or two,
                      # and the harness's own scripts alone name 'reset' or 'host' on ten
# Whole words (clause_words) of a citation rather than a rule: "(user msg #12)", "the user's word, October 5".
CITE_WORDS = frozenset({"user", "users", "word", "words", "message", "messages", "msgs", "january", "february", "march",
                        "april", "june", "july", "august", "september", "october", "november", "december"})


# Words that never say which machine, resource or condition a rule is about: common verbs of state, need
# and validity, and generic nouns. A two-word hit (tool_hit) needs a specific anchor, a matched stem not of
# these: the machine, alias or resource the item names, or another distinctive term of it (its condition
# or clearance terms too). Any description shares 'remains valid' or 'requires machine admission' with a
# rule; shared stems of these alone never hit, however many. Accepted gap: a rule whose only distinctive
# words are listed here hits only on its identifiers, quoted phrases and cites.
GENERIC_WORDS = frozenset("""
remain require need keep stay hold apply become mean include ensure verify confirm check restore return
continue follow provide make take give come show call find look change report record state note say said
valid applicable available possible current specific general certain normal usual necessary enough
result model value status machine host node device system thing case part place time hour minute week
work worker project task user requirement verification restoration validity application""".split())
# Words of the harness's own vocabulary: charters, specs and tools all use them, so they never say which
# rule a tool enforces (a removed power exception shared 'item', 'scope' and 'limit' with nearly every tool).
HARNESS_WORDS = frozenset("""
item items restriction restrictions exception exceptions excepted scope scoped scopes limit limits limited
charter policy policies section sections binding spec specs harness retired retire""".split())
# A value with a unit: '900 W', '2 hours', '50%', '4 chips'. Both forms ('900 w', '900w') are terms.
_VALUE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?(%|[kmg]?w|watts?|v|volts?|°c|ms|secs?|seconds?|mins?|minutes?"
                    r"|hrs?|hours?|days?|weeks?|[kmgt]i?b|chips?|cores?|nodes?|tray(?:s)?|hosts?|boxes)(?!\w)", re.I)
_TOKEN = re.compile(r"\w+")
# Letters and digits joined by '-', bounded by neither: box-a, tt-box-3 (a date has no letter: clause_words).
_HYPHENATED = re.compile(r"(?<![\w-])[a-z0-9]+(?:-[a-z0-9]+)+(?![\w-])")


def _whole(w: str, low: str) -> bool:
    """`w` is in `low` as whole words: no word character right before or after it ('rul' is never in 'rule'),
    nor a '-' when `w` has one (box-a is never in my-box-a or box-a-2)."""
    b = r"[\w-]" if "-" in w else r"\w"
    return re.search(rf"(?<!{b}){re.escape(w)}(?!{b})", low) is not None


def _name(w: str) -> bool:
    """`w` is a machine or resource name or a value: hyphenated (box-a) or holding a digit (rack7, 900w)."""
    return "-" in w or any(c.isdigit() for c in w)


def value_terms(text: str, kept: str = "") -> set[str]:
    """The values with a unit of `text` (_VALUE), lowercased, as '900 w' and '900w', where `kept` (the new
    text and charter) states neither form."""
    keep = " ".join(kept.lower().split())
    out = set()
    for m in _VALUE.finditer(text):
        forms = {f"{m[1]} {m[2].lower()}", f"{m[1]}{m[2].lower()}"}
        if not any(_whole(x, keep) for x in forms):
            out |= forms
    return out


def generic_stems() -> frozenset[str]:
    """The _rule_words stems of GENERIC_WORDS and their -s, -ed, -ing, -ies and -ied forms."""
    global _GENERIC_STEMS
    if _GENERIC_STEMS is None:
        from .coordinator import _stem
        _GENERIC_STEMS = frozenset(_stem(f) for w in GENERIC_WORDS
                                   for f in (w, w + "s", w + "d", w + "ed", w + "ing", w[:-1] + "ing",
                                             w[:-1] + "ies", w[:-1] + "ied"))
    return _GENERIC_STEMS


_GENERIC_STEMS: frozenset[str] | None = None


class WordIndex:
    """Whole words of `lines`, tokenized once: a word that is all word characters is on a line exactly
    when it is one of the line's \\w+ tokens (a dict lookup, no regex per line); any other word is searched
    (_whole) only on the lines that have its leading word part. Each word's lines are kept, so counting
    (distinctive_words) and matching (tool_hit) share one pass."""

    def __init__(self, lines=()):
        self.low: list[str] = []
        self.nonblank = 0
        self._by: dict[str, list[int]] = {}
        self._keys: list[str] | None = None
        self._memo: dict[str, frozenset[int]] = {}
        self.add(lines)

    def add(self, lines) -> None:
        for x in lines:
            i, low = len(self.low), x.lower()
            self.low.append(low)
            self.nonblank += bool(x.strip())
            for t in set(_TOKEN.findall(low)):
                self._by.setdefault(t, []).append(i)
        self._keys, self._memo = None, {}

    def lines_of(self, w: str) -> frozenset[int]:
        """The indexes of the lines that have `w` as whole words (_whole)."""
        if (got := self._memo.get(w)) is not None:
            return got
        head = _TOKEN.match(w)
        if head and head[0] == w:
            got = frozenset(self._by.get(w, ()))
        else:   # a word with other characters: searched on the lines that have its leading word part
            got = frozenset(i for i in (self._by.get(head[0], ()) if head else range(len(self.low)))
                            if _whole(w, self.low[i]))
        self._memo[w] = got
        return got

    def words_on(self, i: int, words) -> list[str]:
        """The words of `words` that line `i` has as whole words, sorted."""
        return sorted(w for w in words if i in self.lines_of(w))

    def hits(self, words) -> dict[int, list[str]]:
        """Line index -> the sorted words of `words` it has as whole words, for the lines with any."""
        out: dict[int, list[str]] = {}
        for w in sorted(words):
            for i in self.lines_of(w):
                out.setdefault(i, []).append(w)
        return out


def distinctive_words(words: set[str], lines: list[str] | tuple | WordIndex) -> set[str]:
    """The words of `words` found (as whole words) on at most COMMON_SHARE of the non-blank `lines`,
    or on at most COMMON_MIN of them: 'work', 'project' or 'fail' of a long item match almost any line.
    `lines` may be a WordIndex, built once for several calls."""
    idx = lines if isinstance(lines, WordIndex) else WordIndex(lines)
    most = max(COMMON_MIN, COMMON_SHARE * idx.nonblank)
    return {w for w in words if len(idx.lines_of(w)) <= most}


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
             rare: set[str] | None = None, on: list[str] | None = None) -> list[str]:
    """What a line names of the old item, each as whole words (_whole: 'rul' is never in 'rule'): an
    identifier, a quoted phrase or user-message cite of `exact` (exact_terms), or else two or more of its
    words, one of them a specific anchor: not generic (generic_stems) and in `rare` (distinctive_words) when
    given; [] when none. `on`: the sorted words of `words` the line has, when already known
    (WordIndex.words_on)."""
    from .restriction_source import CITE_RE
    got = sorted(t for t in ids if _whole(t, line))
    low = line.lower()
    if exact:
        folded = " ".join(low.split())
        cites = {"#" + re.search(r"\d+$", m.group())[0] for m in CITE_RE.finditer(line)}
        got += sorted(t for t in exact if (t in cites if t.startswith("#") else _whole(t, folded)))
    if got:
        return got
    if on is None:
        tokens = set(_TOKEN.findall(low))
        on = sorted(w for w in words if (w in tokens if (h := _TOKEN.match(w)) and h[0] == w else _whole(w, low)))
    got = on
    from .coordinator import _stem   # stems or whole words alike: a word's stem is compared
    anchor = {w for w in got if _stem(w) not in generic_stems()}
    return got if len(got) >= 2 and (anchor if rare is None else rare & anchor) else []


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


def item_changes(old: str, new: str) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """How the Restrictions items of `new` differ from those of `old`: (added items, changed items as
    (old item, the new item it became), removed items). Items are matched by their text compared
    loosely (_loose: bullet, case, spacing and end punctuation aside), so an item in both, in any order,
    is unchanged. A left-over old item became the left-over new item that shares the most of its rule
    words (_widens), or else the one most like it word for word (CHANGED_RATIO or more; any, when
    `old` is one item and one new item is left); with none it was removed. A left-over new item no
    old item became was added, so an insertion's neighbours stay unchanged."""
    from .coordinator import _items, _loose, _rule_words, _widens
    olds = [x for x in (" ".join(old[s:e].split()) for s, e, _ in _items(old)) if x]
    news = [x for x in (" ".join(new[s:e].split()) for s, e, _ in _items(new)) if x]
    left = list(news)
    gone = []
    for o in olds:
        same = next((n for n in left if _loose(n) == _loose(o)), None)
        if same is None:
            gone.append(o)
        else:
            left.remove(same)
    words = lambda x: re.findall(r"[^\s.,;:!?()\"]+", _loose(x))
    like = lambda a, b: difflib.SequenceMatcher(a=words(a), b=words(b), autojunk=False).ratio()
    changed, removed = [], []
    for o in gone:
        near = [n for n in left if _widens(o, _rule_words(n))]
        to = max(near, key=lambda n: len(_rule_words(o) & _rule_words(n)), default="")
        if not to and left:
            to = max(left, key=lambda n: like(o, n))
            to = to if like(o, to) >= CHANGED_RATIO or len(olds) == len(left) == 1 else ""
        if to:
            changed.append((o, to))
        else:
            removed.append(o)
    became = {n for _, n in changed}
    return [n for n in left if n not in became], changed, removed


def replaced_items(body: str, text: str) -> list[tuple[str, str]]:
    """The items of `body` (a quoted Restrictions edit, or a section a `replaces` retired) that its new
    `text` loosens or drops (_loosens), as (old item, the new item it became or ""), from item_changes:
    an unchanged item, in any order, or a pure addition is never one; an item only contained in a new
    item (extended with an exception) is changed."""
    from .coordinator import _loosens
    _, changed, removed = item_changes(body, text)
    return [(o, n) for o, n in changed + [(r, "") for r in removed] if _loosens(o, n)]


AUDIT_BUDGET_S = 5.0   # wall clock of one restriction_tools call: it runs on the daemon's tick thread


_OVER_TASK = re.compile(r"(?<![\w#])(?:task\s*#?|#)(\d+)(?!\d)", re.I)
_OVER_SHA = re.compile(r"(?<![\w-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?![\w-])")
FIX_KINDS = ("code", "harness")   # task kinds that change tools


def landed_fix(p: Project, over: str, end: float | None = None) -> str:
    """What `over` (the retirement note of a Restrictions item) names that already updated the tools: a done
    or landed code or harness task ("task #12", "#12"; a user-message cite is no task), or a commit on HEAD of
    the project root's or the harness's git repo (its first 4 hashes, each git call bounded by the time left
    before `end`, a monotonic deadline). "" when it names none."""
    from .restriction_source import CITE_RE
    text = re.sub(r"\b(?:msgs?|messages?)\s*#\d+", " ", CITE_RE.sub(" ", over), flags=re.I)
    ids = {int(m[1]) for m in _OVER_TASK.finditer(text)}
    if ids:
        from .tried import landed_tasks
        landed = None
        for t in filter(None, (p.db.task(i) for i in sorted(ids))):
            if t["kind"] not in FIX_KINDS:
                continue
            if t["status"] == "done":
                return f"task #{t['id']}"
            landed = landed_tasks(p.db) if landed is None else landed
            if t["id"] in landed:
                return f"task #{t['id']}"
    for sha in list(dict.fromkeys(_OVER_SHA.findall(text.lower())))[:4]:
        for repo in (p.root, p.harness):
            left = 30 if end is None else min(30, end - time.monotonic())
            if left <= 0:
                return ""
            try:
                r = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, "HEAD"],
                                   capture_output=True, timeout=left)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0:
                return f"commit {sha}"
    return ""


def restriction_tools(p: Project, loosened: list[tuple], batch: list[int],
                      notes: list[str] | None = None, budget_s: float | None = None) -> list[str]:
    """After Restrictions items were loosened or removed, name the project's own tools that still carry
    the old wording: files under bin/ and hooks/ (project root and harness), harness/schedules.json,
    and the command and heal check of each enabled schedule, whose lines name, as whole words, an identifier
    of the item's changed clause or two of its words (tool_terms: only the words the change dropped or
    inserted, never words it kept or other items' words; an identifier the current charter still states
    anywhere is not one; a removed or retired item matches only its distinctive words, quoted phrases and
    cites; two words hit only with one distinctive, non-generic word among them: generic_stems),
    each named with the terms it matched. A disabled schedule, in the table or its schedules.json entry, is left out. `loosened`: (old item, new text, over[,
    advisory]), `over` empty for the user's word, else the end that retired the item; `advisory`: the dropped
    items were all unsourced (restriction_source.drops_unsourced), so whoever dropped them and with any `over`,
    one the Brief or a sourced item still states is left out, as is `over` "unsourced", and an `over` naming the
    done fix that already updated the tools (landed_fix: a done or landed code or harness task, a commit on
    HEAD) queues nothing. A file a task of this batch
    names (its title or spec has the file's path or name) is left out. Open tasks' start_when probes and
    schedule prechecks are scanned too. A purely additive item (an `excepts` exception, a `both_hold` text)
    changes no older item and is never passed in. One call is one change (the turn's): at most one
    priority-1 harness task naming every flagged item and its files (one label TASK_LABEL + a key per item:
    an item a task already covers is skipped, so the same change again queues none) and one queued
    restriction_tools event for the next digest. Model-free; it edits no tool. Returns the event texts.
    Bounded by `budget_s` (AUDIT_BUDGET_S) of wall clock: past it the scan fails open, keeps what it found
    (the items' tools named so far still queue the task) and appends a cut-short note to `notes`."""
    from .coordinator import clip
    end = time.monotonic() + (AUDIT_BUDGET_S if budget_s is None else budget_s)
    files: list[tuple[str, str]] = []   # (label, text)
    seen = 0
    cut = ""   # what the budget left unscanned
    for base in (p.root, p.harness):
        for sub in ("bin", "hooks"):
            top = base / sub
            if not top.is_dir() or cut:
                continue
            for f in sorted(top.rglob("*")):
                if seen >= TOOL_SCAN_FILES:
                    break
                if time.monotonic() > end:
                    cut = "reading the tool files"
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
        cmds = [payload.get("command"), payload.get("precheck")] + [heal_block.get(k) for k in ("check", "fix", "suppress")]
        files.append((f"schedule {r['name']}", "\n".join(str(c) for c in cmds if c)))
    from .db import OPEN_TASKS, deferral
    for t in p.db.q(f"SELECT id, labels FROM tasks WHERE {OPEN_TASKS} AND labels LIKE '%start_when:%' LIMIT 500"):
        if when := deferral(t).get("when"):
            files.append((f"task #{t['id']} start_when", when))
    named = " ".join(f"{t['title']} {t['spec']}" for t in (p.db.task(i) for i in batch) if t)
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    idx, starts = WordIndex(), []   # one tokenizing pass over every tool line, shared by all items
    for _, text in files:
        if time.monotonic() > end:
            cut = cut or "indexing the tool lines"
            break
        starts.append(len(idx.low))
        idx.add(text.splitlines())
    files = files[:len(starts)]
    out: list[str] = []
    flagged, labels, checked = [], [], 0   # (how, new text, hits) per changed item with tools still naming it
    from .restriction_source import bare
    for old, new, over, *rest in loosened:
        if cut:
            break
        checked += 1
        if (over == "unsourced" or (rest and rest[0])) and still_stated(charter, old):
            continue   # a dropped advisory copy of a rule the Brief or a sourced item still binds: tools stay right
        key = "\0".join(" ".join(bare(x).lower().split()) for x in (old, new, over))   # cites aside
        label = TASK_LABEL + hashlib.sha256(key.encode()).hexdigest()[:12]
        if label in labels or p.db.q("SELECT 1 FROM tasks WHERE labels LIKE ? LIMIT 1", (f'%"{label}"%',)):
            continue   # a task already covers this change: idempotent per change
        if over and landed_fix(p, over, end):
            continue   # its retirement names the done fix that already updated the tools
        ids, words, exact = tool_terms(old, new, charter, idx)
        rare = distinctive_words(words, idx)
        on = idx.hits(words)
        hits = []
        for (label_, text), at in zip(files, starts):
            if time.monotonic() > end:
                cut = "matching the tool lines"
                break
            line, got = next(((n, g) for n, x in enumerate(text.splitlines(), 1)
                              if (g := tool_hit(x, ids, words, exact, rare, on.get(at + n - 1, [])))),
                             (0, []))
            probe = label_.startswith(("schedule ", "task #"))   # no file: no line number
            if not line or label_ in named or (not probe and Path(label_).name in named):
                continue
            hits.append(label_ + (f":{line}" if not probe else "")
                        + f" ({', '.join(clip(g, 40) for g in got[:4])})")
        if hits:
            how = (f"`over` retired the Restrictions item \"{clip(old, 200)}\" ({clip(over, 150)})" if over
                   else f"The user's word loosened the Restrictions item \"{clip(old, 200)}\"")
            flagged.append((how, new, hits))
            labels.append(label)
    if flagged:   # one task and one event for the whole change, however many items it touched
        items = []
        for how, new, hits in flagged:
            listed = ", ".join(hits[:10]) + (f" and {len(hits) - 10} more" if len(hits) > 10 else "")
            items.append(f"{how}" + (f"; it now reads: \"{clip(new, 400)}\"" if new else "; it was removed")
                         + f". These project tools still name its changed words: {listed}.")
        tid = p.db.add_task(
            "Update project tools still enforcing a changed Restrictions item",
            " ".join(items) + " Update each tool to allow exactly what "
            "the new wording allows and keep enforcing the rest; never loosen past the new text. Add or adjust "
            "the tool's tests if it has any. A tool that does not enforce the item: say so in `summary` and "
            "change nothing. Edit only this project's own tools.",
            kind="harness", tier="standard", priority=1, origin="daemon", labels=labels)
        x = (" ".join(items)[:3000] + f" No task of that batch names them. Queued priority-1 task #{tid} to update "
             "every tool that enforces the old wording; cancel it and say in `summary` why if these do not "
             "enforce it.")
        out.append(x)
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
               (time.time(), "harness", "restriction_tools", "normal", x, "queued"))
    if cut and notes is not None:
        budget = AUDIT_BUDGET_S if budget_s is None else budget_s
        notes.append(f"restriction tools: the tool scan was cut short at its {budget:g} s budget while {cut} "
                     f"({checked} of {len(loosened)} changed Restrictions items reached, {len(starts)} tool files "
                     "indexed); tools past the cut were not checked against the old wording")
    return out


TASK_LABEL = "restriction_tools:"   # + a 12-hex key per change: the queued update task's label


def digest_lines(p: Project) -> list[str]:
    """One line per open restriction_tools update task (at most 3): a guard is behind its charter."""
    from .db import OPEN_TASKS
    rows = p.db.q(f"SELECT id, status, spec FROM tasks WHERE {OPEN_TASKS} AND labels LIKE ? ORDER BY id",
                  (f'%"{TASK_LABEL}%',))
    out = [f"- #{r['id']} ({r['status']}): " + (r["spec"] or "").split(" Update each tool")[0][:400] for r in rows[:3]]
    if len(rows) > 3:
        out.append(f"- and {len(rows) - 3} more open")
    return (["## Guards behind the charter (project tools still enforce a Restrictions item the user changed; "
             "their update task is open: keep it first in line)"] + out) if out else []


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
