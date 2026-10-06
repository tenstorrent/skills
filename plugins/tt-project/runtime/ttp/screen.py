# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Observation screening: decide cheaply whether something a watcher saw deserves the
coordinator's attention. Order: exact dedupe → rules → Jev (when configured) → wake or record.

The coordinator is the expensive component, so everything here errs toward recording an
observation as an issue and waking the coordinator only for new, actionable, important ones.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from . import jevuse
from .db import DB, SEVERITY_RANK
from .providers.jev import JevOutOfFunds

_VOLATILE = [
    (re.compile(r"\b[0-9a-f]{7,64}\b", re.I), "<hex>"),
    (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d)?(\.\d+)?(Z|[+-]\d\d:?\d\d)?\b", re.I), "<ts>"),
    (re.compile(r"/tmp/[^\s'\"]+"), "<tmp>"),
    (re.compile(r"\bv\d+(\.\d+)+"), "<n>"),        # versions like v1.0.0
    # numbers not glued to a preceding letter, so host03, gpu1 and t48 keep their identity;
    # units after a number are masked with it: 30s, 12ms, 4GB
    (re.compile(r"(?<![A-Za-z0-9_])\d+(\.\d+)*"), "<n>"),
    (re.compile(r"\s+"), " "),
]

RULES = [
    ("critical", re.compile(r"\b(segfault|core dumped|out of memory|oom-kill|kernel panic|data loss|"
                            r"corrupt(ed|ion)|outage|down for)\b", re.I)),
    ("high", re.compile(r"\b(fatal|panic|traceback|exception|assert(ion)? fail|hang(s|ing)?|timed? ?out|"
                        r"deadlock|regression|failed|failure|error)\b", re.I)),
    ("normal", re.compile(r"\b(warn(ing)?|deprecat|retry|slow|flaky)\b", re.I)),
]


def normalize(text: str) -> str:
    t = text.strip().lower()
    for rx, rep in _VOLATILE:
        t = rx.sub(rep, t)
    return t[:2000]


def fingerprint(source: str, text: str) -> str:
    return hashlib.sha1(f"{source}\n{normalize(text)}".encode()).hexdigest()[:16]


def rule_severity(text: str) -> str:
    for sev, rx in RULES:
        if rx.search(text):
            return sev
    return "info"


@dataclass
class Verdict:
    wake: bool
    severity: str
    reason: str
    fingerprint: str
    issue_id: int
    screen: str = "rules"   # rules | jev | dedupe
    jev_out_of_funds: bool = False   # Jev refused for lack of credits; the rules decided instead


def screen(db: DB, cfg: dict, source: str, text: str, hint: str | None = None, jev=None,
           rewake_after_s: float | None = None, repeat: bool = False) -> Verdict:
    """Record the observation as an issue and say whether the coordinator should wake for it.

    A known open issue wakes again when `repeat` is set (the watcher says each report is a new
    event), or when it was last seen more than `rewake_after_s` ago (it came back after a quiet
    spell). Without either, a known open issue stays quiet. An observation an active mute covers
    is recorded and counted but never wakes (see mute)."""
    v = _screen(db, cfg, source, text, hint, jev, rewake_after_s, repeat)
    m = count_muted(db, source, text, v.severity)
    if m and v.wake:
        v.wake, v.reason = False, f"muted ({v.reason})"
    return v


def _screen(db: DB, cfg: dict, source: str, text: str, hint: str | None, jev,
            rewake_after_s: float | None, repeat: bool) -> Verdict:
    now = time.time()
    floor = SEVERITY_RANK.get(cfg.get("screen", {}).get("wake_min_severity", "normal"), 1)
    judged: list[tuple[str, str, str, dict]] = []

    def judge() -> tuple[str, str, str, dict]:   # once per observation, and only when an issue is new
        if not judged:
            judged.append(_judge(db, cfg, source, text, hint, jev, floor))
        return judged[0]

    conditions = watcher_conditions(source, text)
    if conditions is None:
        title = text.strip().splitlines()[0][:160] if text.strip() else source
        v = _issue(db, fingerprint(source, text), source, title, None, hint, judge, floor, now,
                   rewake_after_s, repeat)
        v.jev_out_of_funds = any(j[3].get("jev_out_of_funds") for j in judged)
        return v
    verdicts = []
    for subject, cond, cleared in conditions:
        fp = condition_fingerprint(source, subject, cond)
        if cleared:
            verdicts.append(_clear(db, fp, now))
            continue
        title = (f"{subject}: {cond}" if subject else cond)[:160]
        verdicts.append(_issue(db, fp, source, title, title, hint, judge, floor, now, rewake_after_s, repeat))
    found = [v for v in verdicts if v.issue_id]
    best = next((v for v in verdicts if v.wake), None) or (found or verdicts)[0]
    best.severity = max((v.severity for v in found), key=lambda x: SEVERITY_RANK.get(x, 1), default=best.severity)
    best.jev_out_of_funds = any(j[3].get("jev_out_of_funds") for j in judged)
    return best


def _issue(db: DB, fp: str, source: str, title: str, retitle: str | None, hint: str | None, judge, floor: int,
           now: float, rewake_after_s: float | None, repeat: bool) -> Verdict:
    row = db.one("SELECT * FROM issues WHERE fingerprint=?", (fp,))
    if row:
        db.x("UPDATE issues SET last_seen=?, count=count+1, title=COALESCE(?, title) WHERE id=?",
             (now, retitle, row["id"]))
        rank = SEVERITY_RANK.get(row["severity"], 1)
        seen_at = SEVERITY_RANK.get(hint or "", -1)
        if row["status"] == "fixed":
            # Seen again after it was fixed or closed: it wakes when this sighting is at or above the floor.
            sev = hint if seen_at >= 0 else row["severity"]
            db.x("UPDATE issues SET status=?, severity=?, closed=NULL, cleared_why=NULL WHERE id=?",
                 ("open" if sev != "info" else "ignored", sev, row["id"]))
            v = Verdict(SEVERITY_RANK.get(sev, 1) >= floor, sev, "regressed after fix", fp, row["id"], "dedupe")
            return _jev_missed(db, row, v)
        if seen_at > rank and seen_at >= floor:
            # The watcher now rates it higher than before (a condition kept quiet that turned serious).
            db.x("UPDATE issues SET status='open', severity=? WHERE id=?", (hint, row["id"]))
            return _jev_missed(db, row, Verdict(True, str(hint), f"now {hint}", fp, row["id"], "dedupe"))
        reason = "known issue"
        if row["status"] == "open" and rank >= floor:
            if repeat:
                reason = "repeated"
            elif rewake_after_s is not None and now - float(row["last_seen"] or 0) > rewake_after_s:
                reason = f"back after {(now - float(row['last_seen'] or 0)) / 3600:.1f} h quiet"
        return Verdict(reason != "known issue", row["severity"], reason, fp, row["id"], "dedupe")

    severity, verdict_src, reason, info = judge()
    issue_id = db.x("INSERT INTO issues(fingerprint,source,first_seen,last_seen,count,title,severity,status,screen) "
                    "VALUES(?,?,?,?,1,?,?,?,?)", (fp, source, now, now, title, severity,
                                                   "open" if severity != "info" else "ignored",
                                                   json.dumps({"by": verdict_src, "reason": reason, **info})))
    if info.get("jev_call"):
        jevuse.set_ref(db, info["jev_call"], f"issue:{issue_id}")
    return Verdict(SEVERITY_RANK.get(severity, 1) >= floor, severity, reason, fp, issue_id, verdict_src)


JEV_USE = "screen"
JEV_SETTLE_S = 3 * 86400   # a wake Jev skipped counts as right once its issue stayed quiet this long
TASK_MATCH_MIN = 12        # an issue title this long or longer, found in a task's title or spec, makes it about it


def _jev_missed(db: DB, row: dict, v: Verdict) -> Verdict:
    """A known issue waking now, within the settle window, that Jev kept quiet when it was new: that
    skip was wrong."""
    if v.wake:
        try:
            seen = json.loads(row.get("screen") or "{}")
        except ValueError:
            seen = {}
        if isinstance(seen, dict) and seen.get("jev_call") and seen.get("skipped"):
            call = db.one("SELECT settle_at FROM jev_calls WHERE id=?", (int(seen["jev_call"]),))
            if call and (call["settle_at"] is None or time.time() <= float(call["settle_at"])):
                jevuse.resolve(db, int(seen["jev_call"]), False, v.reason)
    return v


def settle_jev(db: DB, now: float | None = None) -> int:
    """Settle the open screening calls that kept a wake from happening. A skip is a miss when, within
    its settle window, the issue woke the coordinator after all (an event with its fingerprint) or a
    task was about it (the issue names the task, or the task's title or spec quotes the issue's
    title); it is right once the window passed without either. Returns how many it settled."""
    now = time.time() if now is None else now
    n = 0
    for c in db.q("SELECT id, ts, ref, settle_at FROM jev_calls WHERE use=? AND outcome IS NULL "
                  "AND settle_at IS NOT NULL AND ref LIKE 'issue:%'", (JEV_USE,)):
        try:
            issue = db.one("SELECT * FROM issues WHERE id=?", (int(str(c["ref"]).split(":", 1)[1]),))
        except ValueError:
            issue = None
        if not issue:
            continue
        start, end = float(c["ts"]), min(now, float(c["settle_at"]))
        why = ""
        ev = db.one("SELECT ts FROM events WHERE fingerprint=? AND ts>? AND ts<=? ORDER BY ts LIMIT 1",
                    (issue["fingerprint"], start, end))
        if ev:
            why = f"the issue woke the coordinator {(float(ev['ts']) - start) / 3600:.1f} h later"
        else:
            title = (issue["title"] or "").strip().lower()
            for t in db.q("SELECT id, title, spec FROM tasks WHERE created>? AND created<=?", (start, end)):
                if t["id"] == issue["task"] or (len(title) >= TASK_MATCH_MIN and title in
                                                f"{t['title'] or ''}\n{t['spec'] or ''}".lower()):
                    why = f"task #{t['id']} was about the issue"
                    break
        if why:
            n += jevuse.resolve(db, int(c["id"]), False, why, now=now)
        elif now > float(c["settle_at"]):
            n += jevuse.resolve(db, int(c["id"]), True, "stayed quiet", now=now)
    return n


def _judge(db: DB, cfg: dict, source: str, text: str, hint: str | None, jev,
           floor: int) -> tuple[str, str, str, dict]:
    """Severity of a new issue: the watcher's hint, else rules, refined by Jev when configured and
    its screening use is on (see jevuse). Each Jev call is logged with the coordinator wake it skipped,
    priced at a low-effort coordinator turn, and settled later (settle_jev)."""
    rules = severity = hint or rule_severity(text)
    verdict_src, reason, info = "rules", f"rule severity {severity}", {}
    if jev is not None and jev.enabled() and jevuse.allowed(db, cfg, JEV_USE):
        try:
            ans = jev.decide(state=f"source: {source}\nobservation:\n{text[:6000]}", questions={
                "actionable": {"type": "noul",
                               "instructions": "Does this observation describe a problem someone should act on?",
                               "criteria": {"true": "A failure, regression, hang, error or request needing action.",
                                            "false": "Routine output, progress, noise, or an already-resolved state."}},
                "severity": {"type": "score", "instructions": "How severe is it for the project?",
                             "criteria": ["informational", "minor", "significant", "critical outage or data loss"]},
            }, purpose="screen")
            decision: dict = {"rules": rules, "answer": None}
            if ans:
                p = float(ans.get("actionable", {}).get("noul", 0.5))
                s = float(ans.get("severity", {}).get("score", 1.0))
                severity = ["info", "normal", "high", "critical"][max(0, min(3, round(s)))]
                if p < 0.35:
                    severity = "info"
                decision = {"rules": rules, "actionable": round(p, 2), "severity": severity,
                            "wake": SEVERITY_RANK.get(severity, 1) >= floor}
                verdict_src, reason = "jev", f"jev actionable={p:.2f} severity={s:.2f}"
            if ans is not None:   # it was called and paid for
                skipped = SEVERITY_RANK.get(rules, 1) >= floor > SEVERITY_RANK.get(severity, 1)
                info = {"skipped": skipped, "jev_call": jevuse.record(
                    db, JEV_USE, decision, getattr(jev, "last_cost", 0.0),
                    avoided_usd=jevuse.low_turn_cost(db, cfg) if skipped else 0.0,
                    settle_s=JEV_SETTLE_S if skipped else None)}
        except JevOutOfFunds:
            # The rules decide in this same pass: screening again would count earlier items twice.
            reason += " (jev out of funds)"
            info = {"jev_out_of_funds": True}
        except Exception as e:  # screening must never take the daemon down
            reason += f" (jev unavailable: {type(e).__name__})"
    return severity, verdict_src, reason, info


# Watcher conditions ---------------------------------------------------------------------------
# A command watcher reports a line like "<subject>: <item>; <item>", where an item may start with
# now / still / changed / cleared. Each item is kept as its own issue, keyed by source, subject and
# the item's kind (counts and changing numbers masked), so a condition reported every run with new
# counts stays one issue, and "cleared: <item>" closes the issue "now <item>" opened.
_MARK = re.compile(r"^(now|still|changed|cleared)\b:?\s*", re.I)
_COUNT = re.compile(r"\s+x\d+\b", re.I)             # "hold x3", "failed checks x2": a count, not an id
_NUM_LIST = re.compile(r"<n>(\s*,\s*<n>)+")     # "chip 8,9,10" and "chip 3" are the same kind
WATCHER_QUIET_CLOSE_S = 24 * 3600
CLEARED_WHY = "the watcher reported it cleared"
QUIET_WHY = "not seen for 24 h"
CLEAN_RUN_WHY = "a watcher run reported nothing"


def watcher_conditions(source: str, text: str) -> list[tuple[str, str, bool]] | None:
    """(subject, item, cleared) for each item of a one-line command-watcher observation; None for
    anything else, which keeps one issue per normalized text."""
    line = text.strip()
    if not source.startswith("watcher:") or not line or "\n" in line:
        return None
    subject, rest = "", line
    if ": " in line:
        head, tail = line.split(": ", 1)
        if not _MARK.match(head + " "):
            subject, rest = head.strip(), tail
    out = []
    for item in rest.split("; "):
        item = item.strip()
        m = _MARK.match(item)
        cond = item[m.end():].strip() if m else item
        if cond:
            out.append((subject, cond, bool(m) and m.group(1).lower() == "cleared"))
    return out or None


def condition_kind(text: str) -> str:
    return _NUM_LIST.sub("<n>", normalize(_COUNT.sub("", text)))


def condition_fingerprint(source: str, subject: str, cond: str) -> str:
    return hashlib.sha1(f"{source}\n{normalize(subject)}\n{condition_kind(cond)}".encode()).hexdigest()[:16]


def _clear(db: DB, fp: str, now: float) -> Verdict:
    row = db.one("SELECT id, severity, status FROM issues WHERE fingerprint=?", (fp,))
    if row and row["status"] == "open":
        db.x("UPDATE issues SET status='fixed', last_seen=?, closed=?, cleared_why=? WHERE id=?",
             (now, now, CLEARED_WHY, row["id"]))
    # A clear never wakes by itself; the items reported with it decide.
    return Verdict(False, row["severity"] if row else "info", "cleared", fp, row["id"] if row else 0, "dedupe")


def close_watcher_issues(db: DB, source: str | None = None, quiet_s: float | None = None,
                         why: str = QUIET_WHY, now: float | None = None) -> int:
    """Close open command-watcher issues: all of `source`'s, or those not seen for `quiet_s`.
    Returns how many closed. Closing never wakes anyone; a later sighting reopens the issue."""
    now = time.time() if now is None else now
    sql, args = "UPDATE issues SET status='fixed', closed=?, cleared_why=? WHERE status='open'", [now, why]
    if source is not None:
        sql, args = sql + " AND source=?", args + [source]
    else:
        sql += " AND source LIKE 'watcher:%'"
    if quiet_s is not None:
        sql, args = sql + " AND last_seen<?", args + [now - quiet_s]
    return db.conn.execute(sql, args).rowcount


# Mutes ----------------------------------------------------------------------------------------
# A known, recurring condition the user was already told about and nothing of ours can fix: its
# observations are still recorded and counted, but do not wake the coordinator until the mute
# ends, when one summary event does.
MUTES_KEY = "observation_mutes"   # kv: list of active mutes, oldest first
MUTE_MIN_MATCH, MUTE_MAX_H, MUTE_MAX = 3, 72, 20
MUTE_BELOW = ("normal", "high", "critical")


def mute(db: DB, source: Any, match: Any, hours: Any, below: Any = None, why: Any = "",
         now: float | None = None) -> dict:
    """Mute observations from `source` whose text contains `match` (any case) and whose severity is
    below `below` (default critical), for `hours`. Muting the same source and match again replaces
    its end, threshold and reason and keeps its count. Raises ValueError on bad input."""
    now = time.time() if now is None else now
    source, match = str(source or "").strip(), str(match or "").strip()
    if not source:
        raise ValueError("observation_mute needs `source`, the observations' source as the digest shows it "
                         "(e.g. watcher:<name>)")
    if len(match) < MUTE_MIN_MATCH:
        raise ValueError(f"observation_mute needs `match`, a piece of the observation's text of at least "
                         f"{MUTE_MIN_MATCH} characters; got {match!r}")
    try:
        h = float(hours)
    except (TypeError, ValueError):
        h = float("nan")
    if not 1 <= h <= MUTE_MAX_H:
        raise ValueError(f"observation_mute needs `hours` from 1 to {MUTE_MAX_H}; got {hours!r}")
    below = str(below or "critical").strip().lower()
    if below not in MUTE_BELOW:
        raise ValueError(f"observation_mute `below` must be one of {', '.join(MUTE_BELOW)}; got {below!r}")
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        old = next((m for m in active if _same_mute(m, source, match)), None)
        if old is None and len(active) >= MUTE_MAX:
            raise ValueError(f"observation_mute rejected: {MUTE_MAX} mutes are already active; let one end first")
        m = {"source": source, "match": match, "below": below, "hours": h, "why": str(why or "").strip()[:300],
             "since": old["since"] if old else now, "until": now + h * 3600,
             "count": old["count"] if old else 0, "last_at": old["last_at"] if old else None}
        db.set_kv(MUTES_KEY, [x for x in active if x is not old] + [m])
    return m


def _same_mute(m: dict, source: str, match: str) -> bool:
    return m["source"].lower() == source.lower() and m["match"].lower() == match.lower()


def mutes(db: DB, now: float | None = None) -> list[dict]:
    """The mutes still in force."""
    now = time.time() if now is None else now
    return [m for m in db.kv(MUTES_KEY, []) or [] if float(m["until"]) > now]


def count_muted(db: DB, source: str, text: str, severity: str, now: float | None = None) -> dict | None:
    """The active mute covering this observation, after counting it there; None when none does."""
    now = time.time() if now is None else now
    rank, low = SEVERITY_RANK.get(severity, 1), text.lower()
    if not db.kv(MUTES_KEY):
        return None
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        for m in active:
            if (float(m["until"]) > now and m["source"].lower() == source.lower() and m["match"].lower() in low
                    and rank < SEVERITY_RANK[m["below"]]):
                m["count"], m["last_at"] = int(m["count"]) + 1, now
                db.set_kv(MUTES_KEY, active)
                return m
    return None


def expire_mutes(db: DB, now: float | None = None) -> list[dict]:
    """End the mutes whose time is up, queuing one summary observation event for each."""
    now = time.time() if now is None else now
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        done = [m for m in active if float(m["until"]) <= now]
        if not done:
            return []
        db.set_kv(MUTES_KEY, [m for m in active if float(m["until"]) > now])
        for m in done:
            n = int(m["count"])
            seen = (f"{n} observation{'' if n == 1 else 's'}, last at {_when(m['last_at'])}" if n
                    else "no observations")
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (now, m["source"], "observation", "info",
                  f"mute ended: muted {m['source']} {m['match']!r} below {m['below']} for {_hours(m)} h: "
                  f"{seen}" + (f". Muted because: {m['why']}" if m.get("why") else ""), "queued"))
    return done


def mute_line(m: dict, now: float | None = None) -> str:
    """One line for the digest and `ttp status`."""
    now = time.time() if now is None else now
    n = int(m["count"])
    seen = f"{n} muted, last at {_when(m['last_at'])}" if n else "none muted yet"
    return (f"{m['source']} {m['match']!r} below {m['below']}: {seen}; ends in "
            f"{max(0.0, (float(m['until']) - now) / 3600):.1f} h" + (f" ({m['why']})" if m.get("why") else ""))


def _hours(m: dict) -> str:
    return f"{(float(m['until']) - float(m['since'])) / 3600:.1f}".rstrip("0").rstrip(".")


def _when(ts: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else "-"
