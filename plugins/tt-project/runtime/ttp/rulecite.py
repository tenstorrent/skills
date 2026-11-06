# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Standing rules index and cited holds: the coordinator checks each hold, wait and delivery route
against the user's standing rules and cites the one it follows.

- index_lines: the digest's 'Standing rules' section, model-free, in every coordinator's context (a
  session's seed and its delta when it changes, every stateless digest). One line each for the
  charter's Restrictions items (R-xxxx) and Policies items (P-xxxx), memory entries marked standing
  (M-xxxx), open user orders (their obligation id, o<n>), and active pauses and holds the user, another
  project or a machine-wide hold set (H-<resource>, H-project). The hex ids hash the item's text, so an
  id stays the same while its rule does. Each line carries its message cite where it has one. The
  section is cut at MAX_LINES lines and INDEX_CHARS characters and has no ages, so a session gets it
  again only when a rule changes.
- check: after a batch is applied (coordinator.apply), each hold-type action (HOLD_TYPES: a task_update
  to blocked or waiting, a start_after or start_when deferral, a plan gate, a new depends_on or
  waits_on, a resource_pause the coordinator set) whose why/text/reason cites no index id, user
  message, safety rule (SAFETY) or dependency (DEPENDS) is recorded. Every hold-type action writes one
  handled HOLD_EVENT event saying whether it cited. Flag only: it never rejects or holds anything.
- digest_lines: the next turn's Course check line 'uncited hold: ...' for those, shown once.
- audit_line / metric: the daily review's 'rule cites (24 h)' line: hold-type actions, the share that
  cite (target 100%), and likely corrections (correction.py) that name a standing rule."""
from __future__ import annotations

import hashlib
import json
import re
import time

from .db import DB

KEY = "uncited_holds"   # kv: [{"ts", "turn", "what", "text", "shown"}], newest last
HOLD_EVENT = "hold_action"
SECTION = "standing_rules"
HEADER = ("## Standing rules (cite the id, a user msg #, a safety rule or the dependency in the why/text of "
          "any hold, deferral, wait, delivery route or held-resource action; `ttp said <topic>` for the "
          "user's newest word; the user's rule beats your preference; never move or work around a hold "
          "the user or another person set)")
MAX_LINES = 40
INDEX_CHARS = 4000
LINE_CHARS = 110
KEEP_S = 7 * 86400
KEEP_N = 100
DAY = 86400
TEXT_CHARS = 80
SHOWN = 3
RULE_WORDS = 3   # content words a correction shares with an index line to name it
HOLD_STATUS = ("blocked", "waiting")
_ID = re.compile(r"\b(?:[RPM]-[0-9a-f]{4}|o\d+|H-[A-Za-z0-9_.:@/-]+)")
_MSG = re.compile(r"\b(?:msgs?|messages?)\s+#(\d+)", re.I)
# A safety rule, named the way the coordinator prompt asks for it (`safety: <kind>`) or in plain words.
# A coordinator's own pause naming "pause" cites nothing: only a pause the user or another project set does.
SAFETY = re.compile(r"\bsafety\s*:|\brestrictions?\b|\block(?:ed|s)?\b|\bdevice hold\b|"
                    r"\bpaused by (?:the user|another|project)\b|\b(?:user|project)(?:'s)? pause\b|"
                    r"\b(?:spend|budget|dollar|daily) cap\b|\brequired checks?\b|\breview before push\b", re.I)
# A dependency whose output the work needs: a task id after a needs word, or a landed:#<id> wait.
DEPENDS = re.compile(r"\b(?:needs?|output|results?|builds? on|uses?|waits? (?:on|for)|until)\b[^.;\n]{0,40}?#\d+|"
                     r"\blanded:#\d+", re.I)


def _sha4(text: str) -> str:
    return hashlib.sha1(" ".join(str(text).split()).lower().encode()).hexdigest()[:4]


def _clip(text: str, n: int = LINE_CHARS) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _cite(text: str) -> str:
    got = _MSG.findall(str(text or ""))
    return f" (msg #{', #'.join(dict.fromkeys(got))})" if got else ""


def _items(body: list[str]) -> list[str]:
    """A charter section's items: each bullet with its continuation lines, or each paragraph."""
    out: list[str] = []
    cur: list[str] = []
    for line in body + [""]:
        bullet = re.match(r"\s{0,3}[-*]\s+", line)
        if (not line.strip() or bullet) and cur:
            out.append(" ".join(cur))
            cur = []
        if line.strip():
            cur.append(line[bullet.end():] if bullet else line.strip())
    return [x for x in out if x.strip() and not x.strip().startswith("(")]


def entries(p, now: float | None = None) -> list[tuple[str, str]]:
    """[(id, line without the id)] of the standing rules, in index order."""
    from . import obligations, projpause, prompts
    now = time.time() if now is None else now
    out: list[tuple[str, str]] = []
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    for head, body in prompts.charter_sections(charter):
        name = head[3:].strip().lower()
        kind = "R" if name.startswith("restriction") else "P" if name.startswith("polic") else ""
        for item in _items(body) if kind else []:
            out.append((f"{kind}-{_sha4(item)}", ("Restriction: " if kind == "R" else "Policy: ")
                        + _clip(item) + _cite(item)))
    for e in p._memory_entries():
        if e.get("standing"):
            body = str(e.get("body") or "")
            out.append((f"M-{_sha4(e['name'])}", f"memory [{e['name']}]: {_clip(body, LINE_CHARS - 30)}{_cite(body)}"))
    for o in obligations.items(p.db):
        order = (o.get("promise") or {}).get("order")
        if order and not (o.get("receipt") or o.get("dropped")):
            out.append((str(o["id"]), f"user order (msg #{order}): {_clip(o.get('text'))}"))
    for name, h in sorted(p.db.paused_resources().items()):
        by = str(h.get("by") or "")
        other = h.get("shared") or h.get("fleet")
        if by in ("coordinator", "daemon") and not other:
            continue
        who = f"project {h['project']}" if other and h.get("project") else "the user" if by == "user" else by or "unknown"
        out.append((f"H-{name}", f"hold set by {who}: {_clip(h.get('reason') or '(no reason given)', 70)}"))
    pause = projpause.current(p.db, now)
    if pause and pause.get("by") not in ("coordinator", "daemon"):
        out.append(("H-project", f"project paused by {projpause.who(pause)}: {_clip(pause.get('reason') or '-', 70)}"))
    return out


def index_lines(p, now: float | None = None) -> list[str]:
    """The digest's Standing rules section: HEADER and one line per rule, cut to MAX_LINES lines and
    INDEX_CHARS characters; [] when there are none."""
    got = entries(p, now)
    if not got:
        return []
    out, size = [HEADER], len(HEADER)
    for i, (rid, line) in enumerate(got):
        row = f"- {rid} {line}"
        if len(out) > MAX_LINES or size + len(row) + 1 > INDEX_CHARS:
            out.append(f"- ({len(got) - i} more not shown: the charter and memory carry them whole)")
            break
        out.append(row)
        size += len(row) + 1
    return out


def hold_type(a: dict) -> str:
    """What makes action `a` hold, defer, serialize or wait on work ('' when nothing does)."""
    if not isinstance(a, dict):
        return ""
    t = a.get("type")
    if t in ("task_add", "task_update"):
        what = [f for f in ("start_after", "start_when", "gate", "depends_on", "waits_on") if a.get(f)]
        if t == "task_update" and a.get("status") in HOLD_STATUS:
            what.insert(0, str(a["status"]))
        return ",".join(what)
    if t == "resource_pause" and a.get("paused") is True:
        return "pause"
    return ""


def _users_pause(db: DB, a: dict) -> bool:
    """A resource_pause the harness recorded as the user's own (their words in the turn)."""
    name = str(a.get("resource") or a.get("machine") or "").strip()
    if a.get("machine"):
        from . import fleethold
        try:
            return str((fleethold.active().get(name) or {}).get("by") or "") == "user"
        except Exception:
            return False
    return str((db.paused_resources(shared=False).get(name) or {}).get("by") or "") == "user"


def cites(a: dict, ids: set[str]) -> str:
    """How action `a` cites what it follows: 'rule <id>', 'msg', 'safety', 'dependency', or ''."""
    text = " ".join(str(a.get(f) or "") for f in ("why", "text", "reason"))
    if hit := next((m for m in (x.rstrip(".:/-") for x in _ID.findall(text)) if m in ids), None):
        return f"rule {hit}"
    if _MSG.search(text):
        return "msg"
    if SAFETY.search(text):
        return "safety"
    waits = str(a.get("waits_on") or "").strip()
    if DEPENDS.search(text) or waits.lower().startswith("ask:") or re.search(r"#\d+", waits):
        return "dependency"   # a wait on an ask's answer or another task; a bare time or probe is no cite
    return ""


def _records(db: DB) -> list[dict]:
    got = db.kv(KEY, []) or []
    return [r for r in got if isinstance(r, dict)]


def _what(a: dict, kind: str) -> str:
    target = f"#{a['id']}" if a.get("id") is not None else str(a.get("resource") or a.get("machine") or a.get("title") or "")
    return f"{_clip(target, 50)} {a.get('type')} {kind}"


def check(p, applied: list[dict], turn=None, now: float | None = None) -> list[str]:
    """After a batch: record each hold-type action in `applied` and flag the ones that cite nothing. A
    coordinator turn (`turn` set) marks flags from earlier turns shown (its digest carried them); a
    turn-less apply marks nothing. Returns the flagged actions' descriptions."""
    db = p.db
    now = time.time() if now is None else now
    holds = [(a, k) for a in applied if (k := hold_type(a)) and not (k == "pause" and _users_pause(db, a))]
    recs = _records(db)
    for r in recs:
        if turn is not None and r.get("turn") != turn:
            r["shown"] = True
    ids = {rid for rid, _ in entries(p, now)} if holds else set()
    flagged = []
    for a, kind in holds:
        how, what = cites(a, ids), _what(a, kind)
        fp = f"{HOLD_EVENT}:{turn}:{what}" if turn is not None else None
        if fp and db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
            continue   # the same turn applied again
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status) VALUES(?,?,?,?,?,?,?,?)",
             (now, "coordinator", HOLD_EVENT, fp, "low",
              f"hold-type action {what}: " + (f"cites {how}" if how else "cites no rule"),
              json.dumps({"cited": bool(how), "how": how, "what": what, "turn": turn}), "handled"))
        if not how:
            text = _clip(" ".join(str(a.get(f) or "") for f in ("why", "text", "reason")) or "(no why)", TEXT_CHARS)
            recs.append({"ts": now, "turn": turn, "what": what, "text": text, "shown": False})
            flagged.append(what)
    if holds or recs:
        db.set_kv(KEY, [r for r in recs if now - float(r.get("ts") or 0) <= KEEP_S][-KEEP_N:])
    return flagged


def digest_lines(db: DB, now: float | None = None) -> list[str]:
    """Course check lines for the uncited hold-type actions not shown yet; [] when there are none."""
    now = time.time() if now is None else now
    got = [r for r in _records(db) if not r.get("shown") and now - float(r.get("ts") or 0) <= DAY]
    out = [f"- uncited hold: {r['what']} (why: \"{r.get('text') or ''}\") | cite the Standing rules id, user msg #, "
           "safety rule or dependency it follows, or lift it" for r in got[:SHOWN]]
    if len(got) > SHOWN:
        out.append(f"- uncited hold: {len(got) - SHOWN} more")
    return out


def metric(p, now: float | None = None, window_s: float = DAY) -> dict:
    """{holds, cited, cited_pct, corrections, naming_rule}: the window's hold-type actions, how many
    cited a rule, safety rule or dependency, and the likely corrections that name a standing rule."""
    from . import correction
    db = p.db
    now = time.time() if now is None else now
    since = now - window_s
    rows = db.q("SELECT data FROM events WHERE kind=? AND ts>=?", (HOLD_EVENT, since))
    cited = sum(bool(json.loads(r["data"] or "{}").get("cited")) for r in rows)
    recs = [r for r in correction._records(db) if float(r.get("ts") or 0) >= since]
    naming = []
    if recs:
        index = entries(p, now)
        ids = {rid for rid, _ in index}
        cited_msgs = {m for _, line in index for m in _MSG.findall(line)}
        words = [correction.ask_words(line) for _, line in index]
        for r in recs:
            row = db.one("SELECT text FROM messages WHERE id=?", (int(r["msg"]),))
            text = str((row or {"text": ""})["text"] or "")
            said = correction.ask_words(text)
            if any(m in ids for m in _ID.findall(text)) or set(_MSG.findall(text)) & cited_msgs \
                    or any(len(said & w) >= RULE_WORDS for w in words):
                naming.append(int(r["msg"]))
    return {"holds": len(rows), "cited": cited, "cited_pct": round(100 * cited / len(rows)) if rows else None,
            "corrections": len(recs), "naming_rule": naming}


def audit_line(p, now: float | None = None, window_s: float = DAY) -> str:
    """The daily review's line: '' when there were no hold-type actions and no corrections."""
    m = metric(p, now, window_s)
    if not m["holds"] and not m["corrections"]:
        return ""
    share = f"{m['cited_pct']}%" if m["cited_pct"] is not None else "-"
    named = ", ".join(f"#{x}" for x in m["naming_rule"][:8])
    return (f"rule cites ({round(window_s / 3600)} h): {m['holds']} hold-type actions, {m['cited']} cite a rule, "
            f"safety rule or dependency ({share}, target 100%); likely corrections naming a standing rule: "
            f"{len(m['naming_rule'])} of {m['corrections']}" + (f" ({named})" if named else ""))
