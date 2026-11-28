"""Older rules a user message overrides: the memory entries, charter items, config keys and code constants
that speak on the same topic, listed the day the user says it so they are retired or rewritten the same day.

`older(p, db, text)` returns [{where: memory|charter|config|code, name, line, value}], at most MAX.
Topic words are the message's subject words (correction.ask_words, stemmed, the project's common talk
dropped); a memory entry or charter item matches by half the topic words (two at least) or a shared anchor, a
config key or an UPPER_SNAKE constant by sharing two topic words or one beside a value equal to a
duration in the message (a constant also beside a name a matched memory entry gives); a value alone never matches.
A number, date, time, size, amount or id ('2026', '24 h', '100M', 'msg-12', '#7') is no topic term or anchor, an
alias alone does not match, and a memory or charter row shows the shared term as its value. Durations are normalised to seconds (`durations`), so 'an hour', '60 min' and '3600 s'
name one value. `note` runs where correction.flag reads a message and stores kv `rulematch:<msg id>`;
`digest_lines` shows it for OPEN_S, the window to retire or rewrite them. `ttp rule-gone` (`gone`, `remaining`)
re-runs the sources for the old value in every unit; once none remains it stores kv `rulematch:<msg id>:gone`,
the digest line closes, and a rule still there after OVERDUE_S reads 'overdue'.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from . import correction, prompts, reask, rulecite

KV = "rulematch:"   # + message id: {"msg", "items": [older()'s rows]}
GONE = ":gone"   # kv rulematch:<msg id>:gone {"ts"}: the rule is gone
CHECKED = "rulematch-checked:"   # + message id: {"at", "left"}: the last overdue check of a message's rules
CHECK_S = 3600
MATCHER = 2   # bump when older() changes: each stored record is re-matched once by the new matcher
MAX = 5
OPEN_S = 48 * 3600
OVERDUE_S = 24 * 3600
NEAR = 4   # words either side of a duration that it is the time of
_SUFFIX = {"MIN": 60, "MINS": 60, "MINUTES": 60, "H": 3600, "HOURS": 3600, "D": 86400, "DAYS": 86400}   # a name's unit, else seconds
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}   # by the unit's first letter
_WORD_UNIT = r"seconds?|secs?|minutes?|mins?|hours?|hrs?|days?"
_A_UNIT = r"minutes?|mins?|hours?|hrs?|days?"   # not 'a second', the ordinal
_DURATION = re.compile(rf"\b(\d+(?:\.\d+)?)\s*({_WORD_UNIT}|[shd]|(?-i:m))\b|\b(an?|one)\s+({_A_UNIT})\b", re.I)   # 'a' or 'an' only before a whole unit word
_CONST = re.compile(r"^\s*([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\s*(?::[^=]+)?=\s*(.+?)\s*(?:#.*)?$")
_NAME_IN_TEXT = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


def durations(text: str) -> set[int]:
    """The durations `text` names, in seconds: 'an hour', '60 min', '3600 s' -> {3600}."""
    return {_sec(m) for m in _DURATION.finditer(text or "")}


def _sec(m: re.Match) -> int:
    n, u, _, w = m.groups()
    return round(float(n or 1) * _UNITS[(u or w)[0].lower()])


def _timed(text: str) -> list[tuple[int, set[str]]]:
    """Each duration `text` names, in seconds, with the words within NEAR words of it (what it is the time of)."""
    return [(_sec(m), _words(" ".join(re.findall(r"[\w-]+", text[:m.start()])[-NEAR:] + re.findall(r"[\w-]+", text[m.end():])[:NEAR])))
            for m in _DURATION.finditer(text or "")]


_ID_REF = re.compile(r"(msgs?|message|tasks?|runs?|ask|issue|note|user|id|no|pst|pdt|utc|cet|gmt|ms|jan|feb|mar|apr|may|jun|jul"
                     r"|aug|sep|oct|nov|dec)[-_:.]\d[\w:.-]*", re.I)   # an id or date word, a separator, digits


def _numeric(tok: str) -> bool:
    """Whether a word or alias is only a number, date, time, size, amount or id: it starts with a digit, '$' or '#'
    ('2026', '24h', '$8000', '#12'), or is an id or date word then digits ('msg-12', 'nov-28').
    A letter-digit mix such as 't3k' or 'h100' is an alias."""
    return bool(re.match(r"[$#]?\d", tok) or _ID_REF.fullmatch(tok))


def _words(text: str) -> set[str]:
    return {reask._stem(w) for w in correction.ask_words(text) if not _numeric(w) and not re.fullmatch(_WORD_UNIT, w)}


def _anchors(text: str) -> set[str]:
    """reask.anchors without task ids and numeric aliases."""
    return {a for a in reask.anchors(text) if not a.startswith("task:") and not (a.startswith("alias:") and _numeric(a[6:]))}


def _topic(db, text: str) -> set[str]:
    return _words(text) - {reask._stem(w) for w in correction._common(db)}


def _same_topic(said: set[str], anc: set[str], item: str) -> tuple[int, set[str], set[str]]:
    """(the topic words `item` shares with the message, 0 when it is not on its topic; those words; shared anchors).
    An alias's own words count once: a shared alias needs another shared word; a shared key or PR does not."""
    words, both = said & _words(item), anc & _anchors(item)
    rest = words - _words(" ".join(a[6:] for a in both if a.startswith("alias:")))
    near = (len(words) >= 2 and len(words) >= 0.5 * len(said) or bool(rest and both)
            or any(not a.startswith("alias:") for a in both))
    return (max(len(words), 1) if near else 0), words, both


def _value(text: str, words: set[str], both: set[str]) -> str:
    """The shared term as `text` writes it: an anchor, else the longest shared topic word."""
    low = text.lower()
    for a in sorted(both):
        i = low.find(a.partition(":")[2])
        if i >= 0:
            return text[i:i + len(a.partition(":")[2])]
    for w in sorted(words, key=lambda w: (-len(w), w)):
        if m := re.search(rf"\b{re.escape(w)}\w*", text, re.I):
            return m.group(0)
    return rulecite._clip(text, 60)


def _seconds(v) -> int | None:
    """A config value or constant's number as seconds, None when it is not a plain number."""
    try:
        return round(float(str(v).replace("_", "")))
    except (ValueError, OverflowError):   # not a number, or inf
        return None


def _flat(d: dict, pre: str = "") -> list[tuple[str, object]]:
    return [kv for k, v in d.items() for kv in (_flat(v, f"{pre}{k}.") if isinstance(v, dict) else [(pre + k, v)])]


def _constants(p, cfg: dict, ref: str | None = None, pat: str = r"^\s*[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\s*(:[^=]+)?=",
               files: tuple = ("*.py",)) -> list[tuple[str, str, str]]:
    """(file:line, NAME, value) of the UPPER_SNAKE assignments in the project's repo at `ref` (its base ref)."""
    ref = ref or str((cfg.get("delivery") or {}).get("base_ref") or "HEAD")
    try:
        r = subprocess.run(["git", "grep", "-nE", pat, ref, "--", *files],
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


def older(p, db, text: str, cap: int | None = MAX) -> list[dict]:
    """The older rules on `text`'s topic, most shared words first, at most `cap`."""
    said, anc, timed = _topic(db, text), _anchors(text), _timed(text)
    if not said:
        return []
    cfg, found, named = p.config(), [], set()
    for e in p._memory_entries():
        n, words, both = _same_topic(said, anc, e["body"])
        if n:
            found.append((n, {"where": "memory", "name": e["name"], "line": rulecite._clip(e["body"]),
                              "value": _value(e["body"], words, both)}))
            named |= set(_NAME_IN_TEXT.findall(e["body"]))
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    for head, body in prompts.charter_sections(charter):
        user_only = head[3:].strip().lower().startswith("restriction")
        for item in rulecite._items(body) if head else []:
            n, words, both = _same_topic(said, anc, item)
            if n:
                found.append((n, {"where": "charter", "name": f"{head[3:].strip()}: {rulecite._clip(item, 40)}",
                                  "line": rulecite._clip(item) + (" (user-only: change only by the user's word)"
                                                                  if user_only else ""), "value": _value(item, words, both)}))
    for key, v in _flat(cfg):
        words = said & {reask._stem(w) for w in re.split(r"[._]", key)}
        dur, shared = any(s == _seconds(v) and words & near for s, near in timed), len(words)
        if shared and (shared >= 2 or dur):   # a topic word, and a second one or the duration beside it
            n = shared + 2 * dur
            found.append((n, {"where": "config", "name": key, "line": f"{key} = {json.dumps(v)}", "value": str(v)}))
    for loc, name, v in _constants(p, cfg):
        words = said & {reask._stem(w) for w in name.lower().split("_")}
        dur, shared = any(s == _seconds(v) and words & near for s, near in timed), len(words)
        n = shared + (name in named) + 2 * dur
        if shared and (shared >= 2 or name in named or dur):   # a topic word in the name, and a second reason
            found.append((n, {"where": "code", "name": name, "line": f"{loc} {name} = {v}",
                                                "value": v}))
    return [x for _, x in sorted(found, key=lambda f: -f[0])[:cap]]


def note(db, m: dict) -> None:
    """Store the older rules the inbound user message `m` overrides (once per message); never raises."""
    if m.get("direction", "in") != "in" or db.kv(KV + str(m["id"])) is not None:
        return
    try:
        from .project import Project
        items = older(Project(Path(db.path).parent.parent), db, m.get("text") or "")
    except Exception:
        return
    db.set_kv(KV + str(m["id"]), {"msg": int(m["id"]), "items": items, "matcher": MATCHER})   # kept even with none: one look per message


def _secs(name: str, v) -> int | None:
    """A value's seconds by its name's unit suffix (HOLD_MIN = 5 -> 300), None when it is no plain number."""
    try:
        return round(float(str(v).strip("'\" ").replace("_", "")) * _SUFFIX.get(re.split(r"[._]", name.upper())[-1], 1))
    except (ValueError, OverflowError):
        return None


def _tip(p, cfg: dict) -> str:
    """The push branch's tip when it exists in the repo, else the base ref."""
    d = cfg.get("delivery") or {}
    for ref in (d.get("push_branch"), d.get("base_ref"), "HEAD"):
        if ref and subprocess.run(["git", "rev-parse", "-q", "--verify", str(ref)], cwd=p.root, capture_output=True).returncode == 0:
            return str(ref)
    return "HEAD"


def remaining(p, old: str | None = None, name: str | None = None, items: list | None = None) -> list[str]:
    """Where the rule still stands, as 'where name: line': `items` (older()'s rows) checked as recorded, else
    every place that holds `old`: a memory entry or charter line naming it (in any unit), a config value or a code
    constant (a duration only beside `name`) or an env default the repo carries, at the push branch's tip."""
    cfg, ref = p.config(), _tip(p, p.config())
    consts = _constants(p, cfg, ref)
    if items is not None:
        mem = {e["name"]: e["body"] for e in p._memory_entries()}
        try:
            charter = " ".join(p.charter_path.read_text().split())
        except OSError:
            charter = ""
        flat = dict(_flat(cfg))
        here = {"memory": lambda i: str(i["value"]) in mem.get(i["name"], ""),
                "charter": lambda i: i["name"].partition(": ")[2].rstrip("…") in charter,
                "config": lambda i: str(flat.get(i["name"])) == str(i["value"]),
                "code": lambda i: any(n == i["name"] and v == i["value"] for _, n, v in consts)}
        return [f"{i['where']} {i['name']}: {i['line']}" for i in items if here.get(i["where"], lambda _: True)(i)]
    needle, dur, out = (old or "").strip().casefold(), durations(old or ""), []
    if not needle:
        return []
    def hit(text: str) -> bool:
        return needle in text.casefold() or bool(dur & durations(text))
    out += [f"memory {e['name']}: {rulecite._clip(e['body'])}" for e in p._memory_entries() if hit(e["body"])]
    try:
        out += [f"charter: {rulecite._clip(l)}" for l in p.charter_path.read_text().splitlines() if hit(l)]
    except OSError:
        pass
    key = (name or "").casefold()   # a duration in config or code counts only beside the name it was set under
    env = _constants(p, cfg, ref, r"^\s*[A-Z][A-Z0-9]*(_[A-Z0-9]+)+=", (":(exclude)*.py",))
    out += [f"config {k} = {json.dumps(v)}" for k, v in _flat(cfg) if not isinstance(v, bool)
            and (str(v).casefold() == needle or (key and key in k.casefold() and _secs(k, v) in dur))]
    out += [f"{kind} {n}: {loc} = {v}" for kind, rows in (("code", consts), ("env", env)) for loc, n, v in rows
            if v.strip("'\" ").casefold() == needle or (name == n and _secs(n, v) in dur)]
    return out


def _rematch(db, rec: dict) -> dict:
    """`rec` keeping only the items the current matcher still finds for its message (as it finds them now), or that
    stand nowhere now (so `gone` still marks them), written back once with its age kept; a record left with no items is closed."""
    if rec.get("matcher") == MATCHER or not rec.get("items"):
        return rec
    try:
        from .project import Project
        p = Project(Path(db.path).parent.parent)
        m = db.one("SELECT text FROM messages WHERE id=?", (rec["msg"],))
        if m is None:
            return rec
        found, had = older(p, db, m["text"] or "", cap=None), {(i["where"], i["name"]) for i in rec["items"]}
        now = {(i["where"], i["name"]) for i in found}
        keep = [i for i in found if (i["where"], i["name"]) in had] + \
            [i for i in rec["items"] if (i["where"], i["name"]) not in now and not remaining(p, items=[i])]
    except Exception:
        return rec
    rec = {**rec, "items": keep, "matcher": MATCHER}
    db.x("UPDATE kv SET value=? WHERE key=?", (json.dumps({k: v for k, v in rec.items() if k != "ts"}), KV + str(rec["msg"])))
    return rec


def gone(p, db, msg: int | None = None, old: str | None = None, name: str | None = None) -> list[str] | None:
    """`remaining` for message `msg` (its recorded rules) or the value `old`; with none left, a message's
    kv `rulematch:<id>:gone` is stored (once). None: no rules are recorded for `msg`, nothing is marked."""
    rec = db.kv(KV + str(msg)) if msg is not None else None
    rec = _rematch(db, rec) if rec is not None else None
    if msg is not None and rec is None:
        return None
    left = remaining(p, items=rec.get("items", [])) if rec is not None else remaining(p, old, name)
    if msg is not None and not left and db.kv(KV + str(msg) + GONE) is None:
        db.set_kv(KV + str(msg) + GONE, {"ts": time.time()})
    return left


def _records(db, now: float) -> list[dict]:
    return [_rematch(db, {**json.loads(r["value"] or "{}"), "ts": r["ts"]}) for r in db.q(
        "SELECT value, ts FROM kv WHERE key LIKE ? AND key NOT LIKE ? AND ts>=? ORDER BY ts", (KV + "%", "%" + GONE, now - OPEN_S))]


def daily_line(db, now: float | None = None) -> str:
    """Rules retired in the last 24 h (the message to its `gone` mark, median hours) and those overdue."""
    now = time.time() if now is None else now
    hours = []
    for r in db.q("SELECT key, ts FROM kv WHERE key LIKE ? AND ts>=?", (KV + "%" + GONE, now - 86400)):
        m = db.one("SELECT ts FROM messages WHERE id=?", (r["key"].removeprefix(KV).removesuffix(GONE),))
        hours += [(r["ts"] - m["ts"]) / 3600] if m else []
    late = [x for x in _records(db, now) if x.get("items") and now - x["ts"] >= OVERDUE_S and db.kv(KV + str(x["msg"]) + GONE) is None]
    med = f" (median message-to-gone {sorted(hours)[len(hours) // 2]:.1f} h)" if hours else ""
    return f"Rules retired today: {len(hours)}{med}; overdue: {len(late)}"


def digest_lines(db, now: float | None = None) -> list[str]:
    """Course check lines for the open `rulematch:` records, MAX per message; a gone rule closes its lines, a
    rule still there after OVERDUE_S reads 'overdue' with where it stands."""
    now = time.time() if now is None else now
    out = []
    for rec in _records(db, now):
        if db.kv(KV + str(rec["msg"]) + GONE) is not None:
            continue
        if now - rec["ts"] >= OVERDUE_S and rec.get("items"):
            seen = db.kv(CHECKED + str(rec["msg"]))   # the git greps run once an hour per message, not per digest
            if seen and 0 <= now - float(seen["at"]) < CHECK_S:
                left = seen["left"]
            else:
                from .project import Project
                try:
                    left = gone(Project(Path(db.path).parent.parent), db, msg=rec["msg"]) or []
                except Exception:
                    left = [f"{i['where']} {i['name']}" for i in rec["items"][:MAX]]
                db.set_kv(CHECKED + str(rec["msg"]), {"at": now, "left": left[:MAX]})
            out += [f"- overdue: user msg {rec['msg']} rule still at {'; '.join(x.split(':')[0] for x in left[:MAX])}"] if left else []
            continue
        out += [f"- user msg {rec['msg']} may retire: {i['where']} {i['name']} = {i['value']}; retire or rewrite it "
                f"today" for i in rec.get("items", [])[:MAX]]
    return out
