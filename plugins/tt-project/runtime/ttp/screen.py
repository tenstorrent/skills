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
    # durations: compound (1d2h, 18h52m, 3m20s) and decimal with a unit (1.5h, 250ms)
    (re.compile(r"(?<![A-Za-z0-9_])(\d+(\.\d+)?(ms|us|[dhms])){1,4}(?![A-Za-z0-9_])", re.I), "<dur>"),
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
           rewake_after_s: float | None = None, repeat: bool = False, lifecycle: str | None = None,
           whole: bool = False, key: str | None = None) -> Verdict:
    """Record the observation as an issue and say whether the coordinator should wake for it.

    A known open issue wakes again when `repeat` is set (the watcher says each report is a new
    event), or when it was last seen more than `rewake_after_s` ago (it came back after a quiet
    spell). Without either, a known open issue stays quiet. An observation an active mute covers
    is recorded and counted but never wakes (see mute). `lifecycle` (RECEIPT or ERROR, from a
    receipt source) is kept on the issue with its subject; see settle_receipts. `whole` (a watcher's JSON
    line with "whole": true) keeps the line as one condition: its text is never split at '; '. `key` (a watcher JSON line's
    "key" field) is the issue's identity instead of its text: the text may change without a new issue."""
    v = _screen(db, cfg, source, text, hint, jev, rewake_after_s, repeat, lifecycle, whole, key)
    m = count_muted(db, source, text, v.severity, whole=whole, key=key)
    if m and v.wake:
        v.wake, v.reason = False, f"muted ({v.reason})"
    return v


def _screen(db: DB, cfg: dict, source: str, text: str, hint: str | None, jev,
            rewake_after_s: float | None, repeat: bool, lifecycle: str | None = None, whole: bool = False,
            key: str | None = None) -> Verdict:
    now = time.time()
    floor = SEVERITY_RANK.get(cfg.get("screen", {}).get("wake_min_severity", "normal"), 1)
    judged: list[tuple[str, str, str, dict]] = []

    def judge() -> tuple[str, str, str, dict]:   # once per observation, and only when an issue is new
        if not judged:
            judged.append(_judge(db, cfg, source, text, hint, jev, floor))
        return judged[0]

    if key:
        title = _first_line(text)[:160] or source
        parts = watcher_conditions(source, text, whole=True) or [("", title, False)]
        subject, _cond, cleared = parts[0]
        if cleared:
            return _clear(db, key_fingerprint(source, key), now)
        v = _issue(db, key_fingerprint(source, key), source, title, title, hint, judge, floor, now,
                   rewake_after_s, repeat, lifecycle, normalize(subject))
        v.jev_out_of_funds = any(j[3].get("jev_out_of_funds") for j in judged)
        return v
    conditions = watcher_conditions(source, text, whole)
    if conditions is None:
        title = text.strip().splitlines()[0][:160] if text.strip() else source
        v = _issue(db, fingerprint(source, text), source, title, None, hint, judge, floor, now,
                   rewake_after_s, repeat, lifecycle)
        v.jev_out_of_funds = any(j[3].get("jev_out_of_funds") for j in judged)
        return v
    verdicts = []
    for subject, cond, cleared in conditions:
        fp = condition_fingerprint(source, subject, cond)
        if cleared:
            verdicts.append(_clear(db, fp, now))
            continue
        title = (f"{subject}: {cond}" if subject else cond)[:160]
        verdicts.append(_issue(db, fp, source, title, title, hint, judge, floor, now, rewake_after_s, repeat,
                               lifecycle, normalize(subject)))
    found = [v for v in verdicts if v.issue_id]
    best = next((v for v in verdicts if v.wake), None) or (found or verdicts)[0]
    best.severity = max((v.severity for v in found), key=lambda x: SEVERITY_RANK.get(x, 1), default=best.severity)
    best.jev_out_of_funds = any(j[3].get("jev_out_of_funds") for j in judged)
    return best


def _issue(db: DB, fp: str, source: str, title: str, retitle: str | None, hint: str | None, judge, floor: int,
           now: float, rewake_after_s: float | None, repeat: bool, lifecycle: str | None = None,
           subject: str | None = None) -> Verdict:
    row = db.one("SELECT * FROM issues WHERE fingerprint=?", (fp,))
    if row:
        db.x("UPDATE issues SET last_seen=?, count=count+1, title=COALESCE(?, title), lifecycle=?, subject=? "
             "WHERE id=?", (now, retitle, lifecycle, subject, row["id"]))
        rank = SEVERITY_RANK.get(row["severity"], 1)
        seen_at = SEVERITY_RANK.get(hint or "", -1)
        if row["status"] == "fixed":
            # Seen again after it was fixed or closed: it wakes when this sighting is at or above the floor.
            sev = hint if seen_at >= 0 else row["severity"]
            db.x("UPDATE issues SET status=?, severity=?, closed=NULL, cleared_why=NULL, opened=? WHERE id=?",
                 ("open" if sev != "info" else "ignored", sev, now, row["id"]))
            v = Verdict(SEVERITY_RANK.get(sev, 1) >= floor, sev, "regressed after fix", fp, row["id"], "dedupe")
            return _jev_missed(db, row, v)
        if seen_at > rank and seen_at >= floor:
            # The watcher now rates it higher than before (a condition kept quiet that turned serious).
            db.x("UPDATE issues SET status='open', severity=?, opened=CASE WHEN status='open' THEN opened ELSE ? END "
                 "WHERE id=?", (hint, now, row["id"]))
            return _jev_missed(db, row, Verdict(True, str(hint), f"now {hint}", fp, row["id"], "dedupe"))
        reason = "known issue"
        if row["status"] == "open" and rank >= floor:
            if repeat:
                reason = "repeated"
            elif rewake_after_s is not None and now - float(row["last_seen"] or 0) > rewake_after_s:
                reason = f"back after {(now - float(row['last_seen'] or 0)) / 3600:.1f} h quiet"
        return Verdict(reason != "known issue", row["severity"], reason, fp, row["id"], "dedupe")

    severity, verdict_src, reason, info = judge()
    issue_id = db.x("INSERT INTO issues(fingerprint,source,first_seen,last_seen,count,title,severity,status,screen,"
                    "lifecycle,subject,opened) VALUES(?,?,?,?,1,?,?,?,?,?,?,?)",
                    (fp, source, now, now, title, severity, "open" if severity != "info" else "ignored",
                     json.dumps({"by": verdict_src, "reason": reason, **info}), lifecycle, subject, now))
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
                rules_wake = SEVERITY_RANK.get(rules, 1) >= floor
                jev_wake = SEVERITY_RANK.get(severity, 1) >= floor
                skipped = rules_wake and not jev_wake
                # It changed the decision only when it skipped a wake or raised one the rules would not have.
                info = {"skipped": skipped, "jev_call": jevuse.record(
                    db, JEV_USE, decision, getattr(jev, "last_cost", 0.0),
                    avoided_usd=jevuse.low_turn_cost(db, cfg) if skipped else 0.0,
                    settle_s=JEV_SETTLE_S if skipped else None, changed=rules_wake != jev_wake)}
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
# counts stays one issue, and "cleared: <item>" closes the issue "now <item>" opened. A JSON line
# with "whole": true is one item instead (watcher_conditions' `whole`).
_MARK = re.compile(r"^(now|still|changed|cleared)\b:?\s*", re.I)
_COUNT = re.compile(r"\s+x\d+\b", re.I)             # "hold x3", "failed checks x2": a count, not an id
_NUM_LIST = re.compile(r"<n>(\s*,\s*<n>)+")     # "chip 8,9,10" and "chip 3" are the same kind
WATCHER_QUIET_CLOSE_S = 24 * 3600
QUIET_RUNS = 3   # a slower watcher's issue closes after this many of its periods unseen, not 24 h
CLEARED_WHY = "the watcher reported it cleared"
QUIET_WHY = "not seen for 24 h"
CLEAN_RUN_WHY = "a watcher run reported nothing"
# Receipt sources: a command schedule with issue_lifecycle "explicit_clear" reports items that stay
# pending until acknowledged (receipts), so the quiet sweep leaves them open. A command watcher's own
# failures are errors under a source of their own (error_source), so no receipt rule covers them: they
# expire as usual and the next successful run that does not report them clears them.
EXPLICIT_CLEAR = "explicit_clear"
RECEIPT, ERROR = "receipt", "error"
REPAIRED_WHY = "a successful watcher run no longer reported it"
REPLACED_WHY = "a newer outcome for the same subject replaced it"


def error_source(source: str) -> str:
    """Where command watcher `source` (watcher:<name>) records its own failures: watcher-error:<name>."""
    return "watcher-error:" + source.split(":", 1)[1]


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def key_fingerprint(source: str, key: str) -> str:
    return hashlib.sha1(f"{source}\nkey\n{key.strip().lower()}".encode()).hexdigest()[:16]


def watcher_conditions(source: str, text: str, whole: bool = False) -> list[tuple[str, str, bool]] | None:
    """(subject, item, cleared) for each item of a command-watcher observation; None for anything
    else, which keeps one issue per normalized text. `whole` (a JSON line with "whole": true) keeps the
    line as one item: it is never split at '; ', and its title is truncated instead. A multi-line
    observation is one item keyed by its first line, so a change in the lines below it opens no new issue."""
    line = text.strip()
    if not source.startswith("watcher:") or not line:
        return None
    if "\n" in line:
        line, whole = _first_line(line), True
    subject, rest = "", line
    if ": " in line:
        head, tail = line.split(": ", 1)
        if not _MARK.match(head + " "):
            subject, rest = head.strip(), tail
    out = []
    for item in ([rest] if whole else rest.split("; ")):
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
                         why: str = QUIET_WHY, now: float | None = None, skip: Any = (),
                         keep_receipts: Any = ()) -> int:
    """Close open command-watcher issues: all of `source`'s, or those not seen for `quiet_s`, except
    those of the sources in `skip` and the receipts of the sources in `keep_receipts`. Returns how
    many closed. Closing never wakes anyone; a later sighting reopens the issue."""
    now = time.time() if now is None else now
    sql, args = "UPDATE issues SET status='fixed', closed=?, cleared_why=? WHERE status='open'", [now, why]
    if source is not None:
        sql, args = sql + " AND source=?", args + [source]
    else:
        sql += " AND (source LIKE 'watcher:%' OR source LIKE 'watcher-error:%')"
    skip = list(skip)
    if skip:
        sql, args = sql + f" AND source NOT IN ({','.join('?' * len(skip))})", args + skip
    keep = list(keep_receipts)
    if keep:
        sql, args = (sql + f" AND NOT (COALESCE(lifecycle,'')=? AND source IN ({','.join('?' * len(keep))}))",
                     args + [RECEIPT] + keep)
    if quiet_s is not None:
        sql, args = sql + " AND last_seen<?", args + [now - quiet_s]
    return db.conn.execute(sql, args).rowcount


def close_quiet_watcher_issues(db: DB, periods: dict[str, float], now: float | None = None,
                               receipts: Any = ()) -> int:
    """Close command-watcher issues not seen for 24 h, or for QUIET_RUNS periods of a watcher that runs
    less often (`periods`: source -> seconds between runs). A daily watcher that prints a pending item
    every run (or misses a run) keeps it open, so it never closes and reopens with a wake each day.
    The receipts of the sources in `receipts` (explicit_clear) never close by time, however long the
    daemon was down; their errors and everything else of theirs do."""
    slow = {src: QUIET_RUNS * float(every) for src, every in periods.items()
            if QUIET_RUNS * float(every) > WATCHER_QUIET_CLOSE_S}
    keep = list(receipts)
    n = close_watcher_issues(db, quiet_s=WATCHER_QUIET_CLOSE_S, now=now, skip=slow, keep_receipts=keep)
    for src, quiet in slow.items():
        n += close_watcher_issues(db, src, quiet_s=quiet, why=f"not seen for {quiet / 3600:g} h", now=now,
                                  keep_receipts=keep)
    return n


def settle_receipts(db: DB, source: str, since: float, subjects: Any, now: float | None = None) -> int:
    """After a successful run of command watcher `source` (it began at `since`): close the errors it no
    longer reported (repaired; under error_source, or under `source` from before errors had their own)
    and, for a receipt source, the receipts of each subject it reported that it no longer reported (a
    newer outcome replaced them). Receipts of subjects it did not mention stay pending. A closed one
    reported again reopens and wakes. Returns how many closed."""
    now = time.time() if now is None else now
    base = "UPDATE issues SET status='fixed', closed=?, cleared_why=? WHERE status='open' AND last_seen<?"
    n = db.conn.execute(base + " AND source IN (?,?) AND lifecycle=?",
                        (now, REPAIRED_WHY, since, error_source(source), source, ERROR)).rowcount
    subjects = sorted(set(subjects))
    if subjects:
        marks = ",".join("?" * len(subjects))
        n += db.conn.execute(base + f" AND source=? AND lifecycle=? AND subject IN ({marks})",
                             (now, REPLACED_WHY, since, source, RECEIPT, *subjects)).rowcount
    return n


# Mutes ----------------------------------------------------------------------------------------
# A known, recurring condition the user was already told about and nothing of ours can fix: its
# observations are still recorded and counted, but do not wake the coordinator until the mute
# ends, when one summary event does. A mute hides noise, never a box or workload that stays down:
# a muted condition still seen escalate_after_h after it was first muted queues one high event
# (MUTE_PERSISTS), once per mute, re-armed when the condition clears and comes back. It clears on a
# "cleared:" item, on a successful watcher run that no longer reports it (settle_mutes), or after
# MUTE_CLEAR_GAP_S unseen.
MUTES_KEY = "observation_mutes"   # kv: list of active mutes, oldest first
MUTE_MIN_MATCH, MUTE_MAX_H, MUTE_MAX = 3, 72, 20
MUTE_BELOW = ("normal", "high", "critical")
MUTE_ESCALATE_H = 2.0   # default escalate_after_h; 0 never escalates (needs a `why` naming who handles it)
MUTE_ESCALATE_MIN_H = 0.5
# A muted condition not seen for this long has cleared: its next sighting starts a new clock. The
# fallback for runs that never settle (failed or killed ones, sources other than command watchers).
MUTE_CLEAR_GAP_S = 3 * 3600
MUTE_CONDS_MAX = 50   # conditions tracked per mute; the longest unseen go first
MUTE_PERSISTS = "under mute: is the expected recovery happening?"   # the escalation's text; effort trigger


def mute(db: DB, source: Any, match: Any, hours: Any, below: Any = None, why: Any = "",
         now: float | None = None, escalate_after_h: Any = None) -> dict:
    """Mute observations from `source` whose text contains `match` (any case) and whose severity is
    below `below` (default critical), for `hours`. A matching condition still seen `escalate_after_h`
    (default MUTE_ESCALATE_H) after it was first muted queues one high event; 0 never does and needs
    a `why`. Muting the same source and match again replaces its end, threshold, reason and
    escalation (kept when not given) and keeps its count and the clocks of its conditions, except
    those that already escalated, which re-arm from now. Raises ValueError on bad input."""
    now = time.time() if now is None else now
    source, match = str(source or "").strip(), str(match or "").strip()
    why = str(why or "").strip()[:300]
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
    esc = None
    if escalate_after_h not in (None, ""):
        try:
            esc = float(escalate_after_h)
        except (TypeError, ValueError):
            esc = float("nan")
        if not (esc == 0 or MUTE_ESCALATE_MIN_H <= esc <= MUTE_MAX_H):
            raise ValueError(f"observation_mute `escalate_after_h` must be 0 (never) or from "
                             f"{MUTE_ESCALATE_MIN_H:g} to {MUTE_MAX_H}; got {escalate_after_h!r}")
        if esc == 0 and not why:
            raise ValueError("observation_mute `escalate_after_h` 0 (never escalate) needs a `why` naming who "
                             "handles the recovery")
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        old = next((m for m in active if _same_mute(m, source, match)), None)
        if old is None and len(active) >= MUTE_MAX:
            raise ValueError(f"observation_mute rejected: {MUTE_MAX} mutes are already active; let one end first")
        if esc is None:
            esc = float(old.get("escalate_after_h", MUTE_ESCALATE_H)) if old else MUTE_ESCALATE_H
        conds = {k: ({**c, "first_at": now, "escalated": False} if c.get("escalated") else c)
                 for k, c in ((old or {}).get("conds") or {}).items()}
        m = {"source": source, "match": match, "below": below, "hours": h, "why": why,
             "since": old["since"] if old else now, "until": now + h * 3600,
             "count": old["count"] if old else 0, "last_at": old["last_at"] if old else None,
             "escalate_after_h": esc, "armed_at": now, "conds": conds}
        db.set_kv(MUTES_KEY, [x for x in active if x is not old] + [m])
    return m


def _same_mute(m: dict, source: str, match: str) -> bool:
    return m["source"].lower() == source.lower() and m["match"].lower() == match.lower()


def mutes(db: DB, now: float | None = None) -> list[dict]:
    """The mutes still in force."""
    now = time.time() if now is None else now
    return [m for m in db.kv(MUTES_KEY, []) or [] if float(m["until"]) > now]


def count_muted(db: DB, source: str, text: str, severity: str, now: float | None = None,
                whole: bool = False, key: str | None = None) -> dict | None:
    """The active mute covering this observation, after counting it there and tracking how long each
    of its conditions has persisted; None when none does."""
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
                _track(db, m, source, text, now, whole, key)
                db.set_kv(MUTES_KEY, active)
                return m
    return None


def _mute_conditions(m: dict, source: str, text: str, whole: bool = False,
                     key: str | None = None) -> list[tuple[str, str, bool]]:
    """(key, text, cleared) for each condition of an observation `m` covers: the items of a command-watcher
    line that contain the match (all of them when only the whole line does), else the whole text."""
    if key:
        parts = watcher_conditions(source, text, whole=True)
        return [(key_fingerprint(source, key), _first_line(text)[:160] or source, bool(parts and parts[0][2]))]
    items = watcher_conditions(source, text, whole)
    if items is None:
        return [(fingerprint(source, text), text.strip().splitlines()[0][:160] if text.strip() else source, False)]
    out = [(condition_fingerprint(source, subj, cond), f"{subj}: {cond}" if subj else cond, cleared)
           for subj, cond, cleared in items]
    return [c for c in out if m["match"].lower() in c[1].lower()] or out


def _track(db: DB, m: dict, source: str, text: str, now: float, whole: bool = False,
           key: str | None = None) -> None:
    """Start, continue or clear the clock of each condition this muted observation reports, and queue
    the one high event for each that persisted past the mute's escalate_after_h."""
    conds = m.setdefault("conds", {})
    armed = float(m.get("armed_at") or m["since"])
    esc_h = float(m.get("escalate_after_h", MUTE_ESCALATE_H))
    for key, item, cleared in _mute_conditions(m, source, text, whole, key):
        c = conds.get(key)
        if cleared:
            if c:
                conds[key] = {"first_at": None, "last_at": now, "escalated": False, "text": item[:160]}
            continue
        if c is None:   # one seen around when the mute began is what it was set for: its clock starts then
            c = {"first_at": armed if now - armed <= MUTE_CLEAR_GAP_S else now, "escalated": False}
        elif c.get("first_at") is None or now - float(c.get("last_at") or 0) > MUTE_CLEAR_GAP_S:
            c = {"first_at": now, "escalated": False}   # it cleared and came back: re-armed
        c.update(last_at=now, text=item[:160])
        conds[key] = c
        age = now - float(c["first_at"])
        if esc_h > 0 and not c["escalated"] and age >= esc_h * 3600:
            c["escalated"] = True
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (now, m["source"], "observation", "high",
                  f"{m['source']} {m['match']!r} has persisted {_h(age)} h {MUTE_PERSISTS} Seen now: {item[:160]}"
                  + (f". Muted because: {m['why']}" if m.get("why") else ""), "queued"))
    if len(conds) > MUTE_CONDS_MAX:
        keep = sorted(conds, key=lambda k: float(conds[k].get("last_at") or 0))[-MUTE_CONDS_MAX:]
        m["conds"] = {k: conds[k] for k in keep}


def settle_mutes(db: DB, source: str, since: float) -> int:
    """After a successful run of command watcher `source` (it began at `since`): each muted condition of
    `source` that run did not report has cleared, as its issue did, so its next sighting starts a new
    clock and one that already asked re-arms. Returns how many cleared."""
    if not db.kv(MUTES_KEY):
        return 0
    n = 0
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        for m in active:
            if m["source"].lower() != source.lower():
                continue
            for c in (m.get("conds") or {}).values():
                if c.get("first_at") is not None and float(c.get("last_at") or 0) < since:
                    c.update(first_at=None, escalated=False)
                    n += 1
        if n:
            db.set_kv(MUTES_KEY, active)
    return n


def mute_ages(m: dict, now: float | None = None) -> list[str]:
    """How long each condition `m` covers has persisted under it ("2.1 h (asked)"), longest first;
    conditions that cleared or went unseen past MUTE_CLEAR_GAP_S are left out."""
    now = time.time() if now is None else now
    live = [c for c in (m.get("conds") or {}).values()
            if c.get("first_at") is not None and now - float(c.get("last_at") or 0) <= MUTE_CLEAR_GAP_S]
    return [f"{_h(now - float(c['first_at']))} h" + (" (asked)" if c.get("escalated") else "")
            for c in sorted(live, key=lambda c: float(c["first_at"]))]


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
    ages = mute_ages(m, now)
    esc = float(m.get("escalate_after_h", MUTE_ESCALATE_H))
    return (f"{m['source']} {m['match']!r} below {m['below']}: {seen}; "
            + (f"persisting {', '.join(ages)}; " if ages else "")
            + (f"asks after {_h(esc * 3600)} h; " if esc > 0 else "never asks; ")
            + f"ends in {max(0.0, (float(m['until']) - now) / 3600):.1f} h" + (f" ({m['why']})" if m.get("why") else ""))


def _h(seconds: float) -> str:
    return f"{seconds / 3600:.1f}".rstrip("0").rstrip(".")


def _hours(m: dict) -> str:
    return f"{(float(m['until']) - float(m['since'])) / 3600:.1f}".rstrip("0").rstrip(".")


def _when(ts: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else "-"
