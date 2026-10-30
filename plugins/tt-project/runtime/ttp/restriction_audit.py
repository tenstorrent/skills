# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Project tools still enforcing a Restrictions item that was loosened or retired.

After the user's word loosens or removes a Restrictions item (a quoted edit, or a `replaces` of a
whole Restrictions section with looser wording), or `over` retires one, the project's own tools
(bin/, hooks/, schedules) may still carry the old rule. restriction_tools names them in a queued
`restriction_tools` event for the next digest; it never edits anything. coordinator.apply() calls it
once per turn, with the turn's tasks, so a tool a task of that batch already names is left out. An
unsourced (advisory) item the coordinator dropped whose words the Brief or a sourced Restrictions item
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
TOOL_SCAN_FILES = 2000   # files read per check at most (bin/ and hooks/ trees)
TOOL_SCAN_BYTES = 262144   # a larger file is not a tool script


def tool_terms(old: str, new: str) -> tuple[set[str], set[str]]:
    """The old item's distinctive terms for restriction_tools: identifiers (_TOOL_TERM_RE) of its
    changed sentences that the new text no longer has, and its rule words (_rule_words) the new text
    dropped (all of the changed sentences' when it dropped fewer than two)."""
    from .coordinator import _changed_sentences, _rule_words
    changed = " ".join(_changed_sentences(old, new)) or old
    ids = {next(g for g in m.groups() if g) for m in _TOOL_TERM_RE.finditer(changed)}
    ids = {t for t in ids if t not in new and not re.fullmatch(r"[\d./-]+", t)}
    words = _rule_words(changed) - _rule_words(new)
    return ids, (words if len(words) >= 2 else _rule_words(changed))


def tool_hit(line: str, ids: set[str], words: set[str]) -> bool:
    """A line names an identifier of the old item, or two of its rule words (as word starts)."""
    if any(t in line for t in ids):
        return True
    low = line.lower()
    return sum(bool(re.search(rf"\b{re.escape(w)}", low)) for w in words) >= 2


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
        ids = {next(g for g in m.groups() if g) for m in _TOOL_TERM_RE.finditer(item)}
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


def restriction_tools(p: Project, loosened: list[tuple[str, str, str]], batch: list[int]) -> list[str]:
    """After Restrictions items were loosened or removed, name the project's own tools that still carry
    the old wording: files under bin/ and hooks/ (project root and harness), harness/schedules.json,
    and the command and heal check of each schedule, whose lines name an identifier of the old item
    or two of its dropped rule words (tool_terms). `loosened`: (old item, new text, over), `over`
    empty for the user's word, else the end that retired the item. A file a task of this batch
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
        files.append((str(sched.relative_to(p.root)), sched.read_text(errors="replace")))
    for r in p.db.q("SELECT name, payload FROM schedules"):
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
    for old, new, over in loosened:
        if over == "unsourced" and still_stated(charter, old):
            continue   # a dropped advisory copy of a rule the Brief or a sourced item still binds: tools stay right
        ids, words = tool_terms(old, new)
        hits = []
        for label, text in files:
            line = next((n for n, x in enumerate(text.splitlines(), 1) if tool_hit(x, ids, words)), 0)
            if not line or label in named or (not label.startswith("schedule ") and Path(label).name in named):
                continue
            hits.append(label + (f":{line}" if not label.startswith("schedule ") else ""))
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
