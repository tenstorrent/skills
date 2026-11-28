"""Older rules a user message overrides: the memory entries, charter items, config keys and code constants
that speak on the same topic, listed the day the user says it so they are retired or rewritten the same day.

`older(p, db, text)` returns [{where: memory|charter|config|code, name, line, value}], at most MAX.
Topic words are the message's subject words (correction.ask_words, stemmed, the project's common talk
dropped); a memory entry or charter item matches by half the topic words (two at least) or a shared anchor, a
config key or an UPPER_SNAKE constant by sharing two topic words or one beside a value equal to a
duration in the message (a constant also by that value alone, or a name in a matched memory entry). Durations are normalised to seconds (`durations`), so 'an hour', '60 min' and '3600 s'
name one value. `note` runs where correction.flag reads a message and stores kv `rulematch:<msg id>`;
`digest_lines` shows it for OPEN_S, the window to retire or rewrite them.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from . import correction, prompts, reask, rulecite

KV = "rulematch:"   # + message id: {"msg", "items": [older()'s rows]}
MAX = 5
OPEN_S = 48 * 3600
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}   # by the unit's first letter
_WORD_UNIT = r"seconds?|secs?|minutes?|mins?|hours?|hrs?|days?"
_A_UNIT = r"minutes?|mins?|hours?|hrs?|days?"   # not 'a second', the ordinal
_DURATION = re.compile(rf"\b(\d+(?:\.\d+)?)\s*({_WORD_UNIT}|[smhd])\b|\b(an?|one)\s+({_A_UNIT})\b", re.I)   # 'a' or 'an' only before a whole unit word
_CONST = re.compile(r"^\s*([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\s*(?::[^=]+)?=\s*(.+?)\s*(?:#.*)?$")
_NAME_IN_TEXT = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


def durations(text: str) -> set[int]:
    """The durations `text` names, in seconds: 'an hour', '60 min', '3600 s' -> {3600}."""
    return {round(float(n or 1) * _UNITS[(u or w)[0].lower()]) for n, u, _, w in _DURATION.findall(text or "")}


def _topic(db, text: str) -> set[str]:
    return {reask._stem(w) for w in correction.ask_words(text)} - {reask._stem(w) for w in correction._common(db)}


def _same_topic(said: set[str], anc: set[str], item: str) -> int:
    """The topic words `item` shares with the message, 0 when it is not on its topic."""
    got = {reask._stem(w) for w in correction.ask_words(item)}
    shared = len(said & got)
    near = shared >= 2 and shared >= 0.5 * len(said) or shared >= 1 and bool(anc & reask.anchors(item))
    return shared if near else 0


def _value(text: str) -> str:
    m = _DURATION.search(text) or re.search(r"\d+(?:\.\d+)?", text)
    return m.group(0) if m else rulecite._clip(text, 60)


def _seconds(v) -> int | None:
    """A config value or constant's number as seconds, None when it is not a plain number."""
    try:
        return round(float(str(v).replace("_", "")))
    except (ValueError, OverflowError):   # not a number, or inf
        return None


def _flat(d: dict, pre: str = "") -> list[tuple[str, object]]:
    return [kv for k, v in d.items() for kv in (_flat(v, f"{pre}{k}.") if isinstance(v, dict) else [(pre + k, v)])]


def _constants(p, cfg: dict) -> list[tuple[str, str, str]]:
    """(file:line, NAME, value) of the UPPER_SNAKE assignments in the project's repo at its base ref."""
    ref = str((cfg.get("delivery") or {}).get("base_ref") or "HEAD")
    try:
        r = subprocess.run(["git", "grep", "-nE", r"^\s*[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\s*(:[^=]+)?=", ref, "--", "*.py"],
                           cwd=p.root, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in r.stdout.splitlines():
        where, _, rest = line.removeprefix(ref + ":").partition(":")
        num, _, code = rest.partition(":")
        if m := _CONST.match(code):
            out.append((f"{where}:{num}", m.group(1), m.group(2)))
    return out


def older(p, db, text: str) -> list[dict]:
    """The older rules on `text`'s topic, most shared words first, at most MAX."""
    said, anc, dur = _topic(db, text), reask.anchors(text), durations(text)
    if not said:
        return []
    cfg, found, named = p.config(), [], set()
    for e in p._memory_entries():
        if n := _same_topic(said, anc, e["body"]):
            found.append((n, {"where": "memory", "name": e["name"], "line": rulecite._clip(e["body"]),
                              "value": _value(e["body"])}))
            named |= set(_NAME_IN_TEXT.findall(e["body"]))
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    for head, body in prompts.charter_sections(charter):
        user_only = head[3:].strip().lower().startswith("restriction")
        for item in rulecite._items(body) if head else []:
            if n := _same_topic(said, anc, item):
                found.append((n, {"where": "charter", "name": f"{head[3:].strip()}: {rulecite._clip(item, 40)}",
                                  "line": rulecite._clip(item) + (" (user-only: change only by the user's word)"
                                                                  if user_only else ""), "value": _value(item)}))
    for key, v in _flat(cfg):
        shared = len(said & {reask._stem(w) for w in re.split(r"[._]", key)})
        if shared and (shared >= 2 or _seconds(v) in dur):   # a topic word, and a second one or the duration
            n = shared + 2 * (_seconds(v) in dur)
            found.append((n, {"where": "config", "name": key, "line": f"{key} = {json.dumps(v)}", "value": str(v)}))
    for loc, name, v in _constants(p, cfg):
        shared = len(said & {reask._stem(w) for w in name.lower().split("_")})
        n = shared + (name in named) + 2 * (_seconds(v) in dur)
        if n and (shared >= 2 or name in named or _seconds(v) in dur):   # one shared word alone is no match
            found.append((n, {"where": "code", "name": name, "line": f"{loc} {name} = {v}",
                                                "value": v}))
    return [x for _, x in sorted(found, key=lambda f: -f[0])[:MAX]]


def note(db, m: dict) -> None:
    """Store the older rules the inbound user message `m` overrides (once per message); never raises."""
    if m.get("direction", "in") != "in" or db.kv(KV + str(m["id"])) is not None:
        return
    try:
        from .project import Project
        items = older(Project(Path(db.path).parent.parent), db, m.get("text") or "")
    except Exception:
        return
    db.set_kv(KV + str(m["id"]), {"msg": int(m["id"]), "items": items})   # kept even with none: one look per message


def digest_lines(db, now: float | None = None) -> list[str]:
    """Course check lines for the open `rulematch:` records, MAX per message."""
    now = time.time() if now is None else now
    out = []
    for r in db.q("SELECT value FROM kv WHERE key LIKE ? AND ts>=? ORDER BY ts", (KV + "%", now - OPEN_S)):
        rec = json.loads(r["value"] or "{}")
        out += [f"- user msg {rec['msg']} may retire: {i['where']} {i['name']} = {i['value']}; retire or rewrite it "
                f"today" for i in rec.get("items", [])[:MAX]]
    return out
