"""Retired-rule echoes: text the user retired that still binds through a leftover copy.

When a charter_update replaces text (`quote`, `replaces`), a memory_add `supersedes` entries or a
user Correction fact names a wrong value, `record` keeps the retired text's distinctive phrases (its
quoted spans, config keys and identifiers, its first sentence; for a Correction, the wrong part when
it is prose). Each coordinator turn `digest_lines` searches, model-free, every project-local place
the old text can still bind (live memory entries, charter sections, schedules' payloads, open tasks'
specs and start_when probes, config values) and lists the hits under RETIRED_HEADER, which carries its
own instruction. It is a notice only: no event, wake, hold or model call. Not searched: the replacing
text itself (its entry, or its words wherever they stand) and the archives (memory/archive, the charter
history). A phrase the replacing text keeps is no phrase of the retired rule. A hit still there a turn
after it was first shown is marked. Accepted gap: a task whose spec quotes the retired rule in order
to remove it is listed too.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

RETIRED_KEY = "retired_rules"        # kv: [{"id", "at", "source", "old", "new", "phrases", "skip", "negated"}]
SEEN_KEY = "retired_echo_seen"       # kv: {"turn": last_coordinator_turn, "hits": {hit key: turns shown}}
RETIRED_DAYS = 30
RETIRED_KEEP = 50
MAX_PHRASES = 8
SECTION_LINES = 10                   # the whole section, its header included
LINE_CHARS = 160
RETIRED_HEADER = ("## Retired-rule echoes (text the user retired still binds here; fix each this turn: memory_forget "
                  "or memory_add `supersedes`, charter_update `quote`, schedule_set, task_update, config_set; a "
                  "notice, it never waits)")

_QUOTED = re.compile(r'"([^"\n]{6,300})"|“([^”\n]{6,300})”|`([^`\n]{4,200})`'
                     r"|(?:(?<=\s)|^)'([^'\n]{12,300})'(?=[\s.,;:)]|$)")
_IDENT = re.compile(r"\b[a-z][a-z0-9]*(?:[._][a-z0-9]+)+\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_NEGATION = re.compile(r"\b(?:not|never|no longer|instead of|rather than|isn't|aren't|wasn't|weren't|retires)\s*$")


def _flat(text: Any) -> str:
    return " ".join(str(text or "").split()).lower()


def phrases(old: str, new: str = "") -> list[str]:
    """The distinctive phrases of retired text `old`, lower-case: quoted and backticked spans, config
    keys and identifiers (a dot or underscore inside), and its first sentence (its first 12 words), at
    most MAX_PHRASES. One the replacing text `new` holds too is left out: both rules say it."""
    keep = _flat(new)
    found: list[str] = []
    for m in _QUOTED.finditer(old or ""):
        found.append(next(g for g in m.groups() if g))
    found += [m.group() for m in _IDENT.finditer(old or "") if len(m.group()) >= 8]
    first = _SENTENCE.split(" ".join((old or "").split()), 1)[0].rstrip(".!?")
    words = first.split()
    if len(words) >= 4:
        found.append(" ".join(words[:12]))
    out: list[str] = []
    for f in map(_flat, found):
        f = f.strip(" .,;:")
        if len(f) >= 6 and f not in out and f not in keep:
            out.append(f)
    return out[:MAX_PHRASES]


def _store(db, rec: dict, now: float) -> None:
    recs = [r for r in db.kv(RETIRED_KEY, []) or [] if now - r.get("at", 0) < RETIRED_DAYS * 86400
            and r.get("id") != rec["id"]]
    db.set_kv(RETIRED_KEY, (recs + [rec])[-RETIRED_KEEP:])


def record(db, source: str, old: str, new: str = "", skip: list[str] = (), now: float | None = None,
           negated: bool = False, found: list[str] | None = None) -> dict | None:
    """Keep retired text `old` (replaced by `new`) to search for. `skip`: place keys (see places) never
    searched for it, such as the replacing entry. `found`: the phrases, when the caller has them.
    `negated`: a match right after a negation ('never <phrase>') agrees with the change, so is no hit.
    None (nothing kept) when the text has no distinctive phrase."""
    now = time.time() if now is None else now
    ps = phrases(old, new) if found is None else [p for p in map(_flat, found) if len(p) >= 6][:MAX_PHRASES]
    if not ps:
        return None
    rec = {"id": hashlib.sha256(f"{source}\n{_flat(old)}".encode()).hexdigest()[:10], "at": now,
           "source": " ".join(source.split())[:200], "old": " ".join((old or "").split())[:600],
           "new": _flat(new)[:4000], "phrases": ps, "skip": list(skip), "negated": negated}
    _store(db, rec, now)
    return rec


def record_correction(db, name: str, text: str, now: float | None = None) -> dict | None:
    """A user Correction entry `name`: its wrong parts (after 'not', 'never', 'no longer', ...) that are
    prose, at least 3 words, are retired text. A bare value ('not low') is left to correction.repeated."""
    from .correction import _HEAD, _WRONG, _WRONG_END
    head = _HEAD.match(text or "")
    if not head:
        return None
    body = text[head.end():]
    wrong = [_WRONG_END.split(m.group(1))[0].strip() for m in _WRONG.finditer(body)]
    wrong = [w for w in wrong if len(w.split()) >= 3]
    return record(db, f"memory [{name}] (user correction)", "; ".join(wrong), skip=[f"memory:{name}"], now=now,
                  negated=True, found=wrong) if wrong else None


def places(p) -> list[tuple[str, str, str]]:
    """Every project-local place retired text can still bind, as (key, label, text): live memory entries
    (the one store both workers' and the coordinator's copies come from), charter sections, schedules'
    payload strings and descriptions, open tasks' specs and start_when probes, config string values."""
    from .db import TERMINAL_TASK_STATES, deferral
    from .prompts import charter_sections
    out: list[tuple[str, str, str]] = []
    for e in p._memory_entries():
        out.append((f"memory:{e['name']}", f"memory [{e['name']}]", e["body"]))
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    for head, body in charter_sections(charter):
        name = head.lstrip("#").strip() or "(top)"
        out.append((f"charter:{name}", f"charter section '{name}'", "\n".join(body)))

    def walk(key: str, label: str, val: Any, path: str) -> None:
        if isinstance(val, str):
            out.append((f"{key}:{path}", f"{label} {path}".strip(), val))
        elif isinstance(val, dict):
            for k, v in val.items():
                walk(key, label, v, f"{path}.{k}" if path else str(k))
        elif isinstance(val, list):
            for i, v in enumerate(val):
                walk(key, label, v, f"{path}[{i}]")
    for s in p.db.q("SELECT name, description, payload FROM schedules ORDER BY name"):
        try:
            payload = json.loads(s["payload"] or "{}")
        except ValueError:
            payload = s["payload"]
        walk(f"schedule:{s['name']}", f"schedule {s['name']}", {"description": s["description"] or "",
                                                                  "payload": payload}, "")
    for t in p.db.q(f"SELECT id, spec, labels FROM tasks WHERE status NOT IN "
                    f"({','.join('?' * len(TERMINAL_TASK_STATES))}) ORDER BY id", TERMINAL_TASK_STATES):
        out.append((f"task:{t['id']}:spec", f"task #{t['id']} spec", t["spec"] or ""))
        when = deferral(t).get("when")
        if when:
            out.append((f"task:{t['id']}:start_when", f"task #{t['id']} start_when", when))
    try:
        cfg = p.config()
    except Exception:   # a broken config is reported elsewhere; the notice never fails the turn
        cfg = {}
    walk("config", "config", cfg, "")
    return out


def _line(text: str, phrase: str) -> str:
    """The line of `text` holding `phrase`, clipped around it."""
    for ln in text.splitlines():
        if phrase in _flat(ln):
            ln = " ".join(ln.split())
            break
    else:
        ln = " ".join(text.split())
    at = ln.lower().find(phrase.split()[0]) if phrase.split() else 0
    start = max(0, min(at - 40, len(ln) - LINE_CHARS)) if len(ln) > LINE_CHARS else 0
    cut = ln[start:start + LINE_CHARS]
    return ("..." if start else "") + cut + ("..." if start + LINE_CHARS < len(ln) else "")


def _hit(rec: dict, text: str) -> str:
    """The first phrase of `rec` found in `text` outside its replacing text ('' when none)."""
    flat = _flat(text)
    if rec.get("new") and len(rec["new"]) >= 6:
        flat = flat.replace(rec["new"], " ")
    for ph in rec.get("phrases") or []:
        i = flat.find(ph)
        while i >= 0:
            if not (rec.get("negated") and _NEGATION.search(flat[max(0, i - 20):i])):
                return ph
            i = flat.find(ph, i + 1)
    return ""


def scan(p, now: float | None = None) -> list[dict]:
    """The retired texts' echoes: [{"key", "label", "line", "phrase", "source"}], newest retirement first."""
    now = time.time() if now is None else now
    recs = [r for r in p.db.kv(RETIRED_KEY, []) or [] if now - r.get("at", 0) < RETIRED_DAYS * 86400]
    if not recs:
        return []
    where = places(p)
    out, seen = [], set()
    for rec in sorted(recs, key=lambda r: -r.get("at", 0)):
        skip = set(rec.get("skip") or ())
        for key, label, text in where:
            if key in skip or (rec["id"], key) in seen:
                continue
            ph = _hit(rec, text)
            if ph:
                seen.add((rec["id"], key))
                out.append({"key": f"{rec['id']}:{key}", "label": label, "line": _line(text, ph), "phrase": ph,
                            "source": rec.get("source") or ""})
    return out


def digest_lines(p, now: float | None = None) -> list[str]:
    """The digest's Retired-rule echoes section, at most SECTION_LINES lines; [] when nothing echoes. A
    hit already shown in an earlier turn is marked. Never posts, wakes or holds anything."""
    hits = scan(p, now)
    db = p.db
    turn = db.kv("last_coordinator_turn", 0) or 0
    old = db.kv(SEEN_KEY, {}) or {}
    before = old.get("hits") or {}
    fresh = old.get("turn") != turn   # a second digest within one turn counts no turn
    counts = {h["key"]: (before.get(h["key"], 0) + 1 if fresh else before.get(h["key"]) or 1) for h in hits}
    if counts != before or old.get("turn") != turn:
        db.set_kv(SEEN_KEY, {"turn": turn, "hits": counts})
    if not hits:
        return []
    room = SECTION_LINES - 1
    shown = hits if len(hits) <= room else hits[:room - 1]
    lines = [RETIRED_HEADER]
    for h in shown:
        n = counts[h["key"]]
        lines.append(f"- {h['label']}: \"{h['line']}\" (retired: {h['source']}; matches \"{h['phrase'][:60]}\")"
                     + (f" [still here, {n} turns]" if n >= 2 else ""))
    if len(shown) < len(hits):
        lines.append(f"- ... and {len(hits) - len(shown)} more; fix these and the rest show next turn")
    return lines
