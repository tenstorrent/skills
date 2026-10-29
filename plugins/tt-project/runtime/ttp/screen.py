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
from dataclasses import dataclass, field
from typing import Any

from . import jevuse, throughmute, timefmt, watchdup
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
    woke: list[int] = field(default_factory=list)   # the issues this wake is for (their last_woken moves)


def screen(db: DB, cfg: dict, source: str, text: str, hint: str | None = None, jev=None,
           rewake_after_s: float | None = None, repeat: bool = False, lifecycle: str | None = None,
           whole: bool = False, key: str | None = None, episode: str | None = None,
           pin: bool = False) -> Verdict:
    """Record the observation as an issue and say whether the coordinator should wake for it.

    A known open issue wakes again when `repeat` is set (the watcher says each report is a new
    event), or when it last woke the coordinator `rewake_after_s` or more ago (a reminder every
    `rewake_after_s` while it is still reported). Without either, a known open issue stays quiet. An observation an active mute covers
    is recorded and counted but never wakes (see mute). `lifecycle` (RECEIPT or ERROR, from a
    receipt source) is kept on the issue with its subject; see settle_receipts. `whole` (a watcher's JSON
    line with "whole": true) keeps the line as one condition: its text is never split at '; '. `key` (a watcher JSON line's
    "key" field) is the issue's identity instead of its text: the text may change without a new issue.
    A keyed issue a run that reported nothing closed, raised again under the same key within
    `screen.reraise_window_h` of that close at the same or a lower severity, only counts: it stays
    closed and wakes nobody (a watcher re-sending a known line each wake). A higher severity reopens and wakes.
    The same holds for a keyed watcher error (watcher-error:<name>) a successful run repaired (REPAIRED_WHY).
    `episode` (a keyed line's "episode" field) names one occurrence of the keyed condition: a keyed issue
    that recorded another episode is closed as superseded and a new issue opens, judged as any new one
    (no reraise dedupe). The same episode, or none, changes nothing; an issue without one adopts it.
    `pin` (a watcher line's "pin_severity": true, with a valid `hint`) keeps the hint as the severity:
    Jev is not asked (it could only lower it) and no mute covers the observation."""
    pin = pin and hint in SEVERITY_RANK
    v = _screen(db, cfg, source, text, hint, jev, rewake_after_s, repeat, lifecycle, whole, key, episode, pin)
    if not pin and count_muted(db, source, text, v.severity, whole=whole, key=key) and v.wake:
        v.wake, v.reason = False, f"muted ({v.reason})"
    if v.wake and v.woke:
        # Only a wake that happens moves the clock: a muted one reminds once the mute is over.
        db.x(f"UPDATE issues SET last_woken=? WHERE id IN ({','.join('?' * len(v.woke))})",
             (time.time(), *v.woke))
    return v


def _screen(db: DB, cfg: dict, source: str, text: str, hint: str | None, jev,
            rewake_after_s: float | None, repeat: bool, lifecycle: str | None = None, whole: bool = False,
            key: str | None = None, episode: str | None = None, pin: bool = False) -> Verdict:
    now = time.time()
    floor = SEVERITY_RANK.get(cfg.get("screen", {}).get("wake_min_severity", "normal"), 1)
    judged: list[tuple[str, str, str, dict]] = []

    def judge() -> tuple[str, str, str, dict]:   # once per observation, and only when an issue is new
        if not judged:
            judged.append(_judge(db, cfg, source, text, hint, jev, floor, pin))
        return judged[0]

    if key:
        title = _first_line(text)[:160] or source
        parts = watcher_conditions(source, text, whole=True) or [("", title, False)]
        subject, _cond, cleared = parts[0]
        if cleared:
            return _clear(db, key_fingerprint(source, key), now)
        fp = key_fingerprint(source, key)
        episode = (episode or "").strip()[:EPISODE_MAX] or None
        if episode:
            _supersede(db, fp, episode, now)
        v = _issue(db, fp, source, title, title, hint, judge, floor, now,
                   rewake_after_s, repeat, lifecycle, normalize(subject), reraise_window_s(cfg), key.strip(),
                   episode)
        v.jev_out_of_funds = any(j[3].get("jev_out_of_funds") for j in judged)
        v.woke = [v.issue_id] if v.wake and v.issue_id else []
        return v
    conditions = watcher_conditions(source, text, whole)
    if conditions is None:
        title = text.strip().splitlines()[0][:160] if text.strip() else source
        v = _issue(db, fingerprint(source, text), source, title, None, hint, judge, floor, now,
                   rewake_after_s, repeat, lifecycle)
        v.jev_out_of_funds = any(j[3].get("jev_out_of_funds") for j in judged)
        v.woke = [v.issue_id] if v.wake and v.issue_id else []
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
    best.woke = [v.issue_id for v in verdicts if v.wake and v.issue_id]
    return best


def _issue(db: DB, fp: str, source: str, title: str, retitle: str | None, hint: str | None, judge, floor: int,
           now: float, rewake_after_s: float | None, repeat: bool, lifecycle: str | None = None,
           subject: str | None = None, reraise_s: float = 0, watch_key: str | None = None,
           episode: str | None = None) -> Verdict:
    row = db.one("SELECT * FROM issues WHERE fingerprint=?", (fp,))
    if row:
        db.x("UPDATE issues SET last_seen=?, count=count+1, title=COALESCE(?, title), lifecycle=?, subject=?, "
             "episode=COALESCE(episode, ?) WHERE id=?", (now, retitle, lifecycle, subject, episode, row["id"]))
        rank = SEVERITY_RANK.get(row["severity"], 1)
        seen_at = SEVERITY_RANK.get(hint or "", -1)
        if row["status"] == "fixed":
            # Seen again after it was fixed or closed: it wakes when this sighting is at or above the floor.
            sev = hint if seen_at >= 0 else row["severity"]
            why = row.get("cleared_why")
            # A keyed watcher error a later run repaired flaps the same way (a tunnel down while a host sleeps).
            flap = why == CLEAN_RUN_WHY or (why == REPAIRED_WHY and bool(watch_key)
                                            and source.startswith("watcher-error:"))
            if reraise_s > 0 and not repeat and flap and row.get("closed") is not None \
                    and now - float(row["closed"]) <= reraise_s and SEVERITY_RANK.get(sev, 1) <= rank:
                # Closed by a quiet (or repairing) run moments ago and sent again unchanged: counted, not reopened.
                return Verdict(False, row["severity"], f"back within {reraise_s / 3600:g} h of a "
                               + ("quiet run" if why == CLEAN_RUN_WHY else "repairing run"), fp, row["id"], "dedupe")
            history = json.loads(row.get("history") or "[]")
            history.append({"ts": now, "action": "reopened", "by": "a new sighting", "why": "regressed after fix"})
            db.x("UPDATE issues SET status=?, severity=?, closed=NULL, cleared_why=NULL, opened=?, reopens=?, "
                 "history=? WHERE id=?",
                 ("open" if sev != "info" else "ignored", sev, now, _reopens(row, now), json.dumps(history), row["id"]))
            v = Verdict(SEVERITY_RANK.get(sev, 1) >= floor, sev, "regressed after fix", fp, row["id"], "dedupe")
            return _jev_missed(db, row, v)
        if seen_at > rank and seen_at >= floor:
            # The watcher now rates it higher than before (a condition kept quiet that turned serious).
            db.x("UPDATE issues SET status='open', severity=?, opened=CASE WHEN status='open' THEN opened ELSE ? END, "
                 "reopens=? WHERE id=?",
                 (hint, now, row.get("reopens") if row["status"] == "open" else _reopens(row, now), row["id"]))
            return _jev_missed(db, row, Verdict(True, str(hint), f"now {hint}", fp, row["id"], "dedupe"))
        reason = "known issue"
        if row["status"] == "open" and rank >= floor:
            if repeat:
                reason = "repeated"
            elif rewake_after_s is not None:
                # Counted from its last wake, not its last sighting: a condition reported on every run
                # never goes quiet, yet still reminds every rewake_after_s while it holds.
                woken = row.get("last_woken")
                woken = float(woken if woken is not None else row.get("opened") or row["first_seen"] or 0)
                quiet = now - float(row["last_seen"] or 0)
                if now - woken >= rewake_after_s:
                    reason = (f"back after {quiet / 3600:.1f} h quiet" if quiet >= rewake_after_s
                              else f"still open {(now - woken) / 3600:.1f} h after its last wake")
        return Verdict(reason != "known issue", row["severity"], reason, fp, row["id"], "dedupe")

    severity, verdict_src, reason, info = judge()
    issue_id = db.x("INSERT INTO issues(fingerprint,source,first_seen,last_seen,count,title,severity,status,screen,"
                    "lifecycle,subject,opened,watch_key,episode) VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?)",
                    (fp, source, now, now, title, severity, "open" if severity != "info" else "ignored",
                     json.dumps({"by": verdict_src, "reason": reason, **info}), lifecycle, subject, now, watch_key,
                     episode))
    if info.get("jev_call"):
        jevuse.set_ref(db, info["jev_call"], f"issue:{issue_id}")
    return Verdict(SEVERITY_RANK.get(severity, 1) >= floor, severity, reason, fp, issue_id, verdict_src)


def _reopens(row: dict, now: float) -> str | None:
    """A keyed issue's reopen times with this one (watchdup counts its churn); others keep none."""
    return watchdup.reopened(row.get("reopens"), now) if row.get("watch_key") else row.get("reopens")


def _supersede(db: DB, fp: str, episode: str, now: float) -> None:
    """A keyed issue that recorded another episode is over: close it (an already closed one keeps its
    close time) and move it off the key's fingerprint, so the new episode opens an issue of its own."""
    row = db.one("SELECT id, episode FROM issues WHERE fingerprint=?", (fp,))
    if row and row["episode"] and row["episode"] != episode:
        db.x("UPDATE issues SET fingerprint=?, status='fixed', closed=COALESCE(closed, ?), cleared_why=? WHERE id=?",
             (f"{fp}~{row['id']}", now, SUPERSEDED_WHY, row["id"]))


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
           floor: int, pin: bool = False) -> tuple[str, str, str, dict]:
    """Severity of a new issue: the watcher's hint, else rules, refined by Jev when configured and
    its screening use is on (see jevuse). Jev may lower a hinted severity but never raise it, nor judge a pinned one. Each Jev call is logged with the coordinator wake it skipped,
    priced at a coordinator turn, and settled later (settle_jev)."""
    rules = severity = hint or rule_severity(text)
    verdict_src, reason, info = "rules", f"rule severity {severity}", {}
    if pin:   # the watcher pinned its hint: Jev could only lower it, so it is not asked
        return severity, verdict_src, f"pinned severity {severity}", info
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
                capped = bool(hint) and SEVERITY_RANK.get(severity, 1) > SEVERITY_RANK.get(hint, 1)
                if capped:   # the watcher's explicit hint is a ceiling: Jev may lower it, never raise it
                    severity = hint
                decision = {"rules": rules, "actionable": round(p, 2), "severity": severity,
                            "wake": SEVERITY_RANK.get(severity, 1) >= floor}
                verdict_src, reason = "jev", f"jev actionable={p:.2f} severity={s:.2f}" + (
                    f" (capped at the watcher's hint {hint})" if capped else "")
            if ans is not None:   # it was called and paid for
                rules_wake = SEVERITY_RANK.get(rules, 1) >= floor
                jev_wake = SEVERITY_RANK.get(severity, 1) >= floor
                skipped = rules_wake and not jev_wake
                # It changed the decision only when it skipped a wake or raised one the rules would not have.
                info = {"skipped": skipped, "jev_call": jevuse.record(
                    db, JEV_USE, decision, getattr(jev, "last_cost", 0.0),
                    avoided_usd=jevuse.turn_cost(db, cfg) if skipped else 0.0,
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
SUPERSEDED_WHY = "superseded by a new episode"
EPISODE_MAX = 200   # a watcher's episode string is kept to this many characters
# Receipt sources: a command schedule with issue_lifecycle "explicit_clear" reports items that stay
# pending until acknowledged (receipts), so the quiet sweep leaves them open. A command watcher's own
# failures are errors under a source of their own (error_source), so no receipt rule covers them: they
# expire as usual and the next successful run that does not report them clears them.
EXPLICIT_CLEAR = "explicit_clear"
RECEIPT, ERROR = "receipt", "error"
REPAIRED_WHY = "a successful watcher run no longer reported it"
REPLACED_WHY = "a newer outcome for the same subject replaced it"


RERAISE_WINDOW_H = 6


def reraise_window_s(cfg: dict) -> float:
    """screen.reraise_window_h in seconds (default RERAISE_WINDOW_H; null, 0 or junk: off)."""
    hours = (cfg.get("screen") or {}).get("reraise_window_h", RERAISE_WINDOW_H)
    try:
        return max(0.0, float(hours) * 3600) if hours is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


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


def find_issue(db: DB, ref: str) -> dict | None:
    """The issue `ref` names: its id (`#` optional), its fingerprint, or its key (a keyed watcher line's
    `key`). A key on several issues names its open one; ValueError when that is still ambiguous."""
    ref = str(ref).strip()
    if re.fullmatch(r"#?\d+", ref):
        return db.one("SELECT * FROM issues WHERE id=?", (int(ref.lstrip("#")),))
    row = db.one("SELECT * FROM issues WHERE fingerprint=?", (ref,))
    if row:
        return row
    rows = db.q("SELECT * FROM issues WHERE watch_key=? ORDER BY id", (ref,))
    live = [r for r in rows if r["status"] == "open"] or rows
    if len(live) > 1:
        raise ValueError(f"key {ref!r} names issues " + ", ".join(f"#{r['id']} ({r['source']})" for r in live)
                         + ": give the id")
    return live[0] if live else None


def close_issue(db: DB, ref: str, why: str, who: str, now: float | None = None) -> dict:
    """Close an issue at someone's word (`ttp issues close`): record when, by whom and why in its history
    and post it to the activity feed. Closing only ends this sighting: a condition still live is seen
    again at its next detection and reopens the issue (_issue), so a close never hides it. Returns the
    row as closed, with "already" set when it was closed before (nothing written). ValueError for an
    empty why or an issue that does not exist."""
    why = (why or "").strip()
    if not why:
        raise ValueError("closing an issue requires a non-empty --why")
    now = time.time() if now is None else now
    with db.tx():
        row = find_issue(db, ref)
        if row is None:
            raise ValueError(f"no issue {ref!r} (an id, fingerprint or key as `ttp issues list --all` shows it)")
        if row["status"] == "fixed":
            return {**row, "already": True}
        history = json.loads(row.get("history") or "[]")
        history.append({"ts": now, "action": "closed", "by": who, "why": why[:500]})
        cleared = f"closed by {who}: {why}"[:500]
        db.x("UPDATE issues SET status='fixed', closed=?, cleared_why=?, history=? WHERE id=?",
             (now, cleared, json.dumps(history), row["id"]))
        db.post("out", f"Closed issue #{row['id']} ({row['source']}: {(row['title'] or '')[:120]}) by {who}: "
                f"{why[:300]}. A new sighting reopens it.", chat=None, kind="info", severity="low",
                ref=f"issue:{row['id']}")
        return {**row, "status": "fixed", "closed": now, "cleared_why": cleared, "history": history}


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
# MUTE_CLEAR_GAP_S unseen. A mute ends when its time is up or, with an `end_when` probe (read-only,
# like a resource pause's: exit 0 = over; 1, 75 or 255 = not yet; run by pauseends.MuteEnds), when
# its probe passes. A broken probe is reported once and the mute keeps its time. However often it is
# extended, no mute outlasts MUTE_CAP_S from when it began: its end then makes the coordinator look again.
MUTES_KEY = "observation_mutes"   # kv: list of active mutes, oldest first
MUTE_MIN_MATCH, MUTE_MAX_H, MUTE_MAX = 3, 72, 20
MUTE_SLOTS_NEAR = 5   # the digest names the first mute to end once this few or fewer slots are free (slots_line)
MUTE_BELOW = ("normal", "high", "critical")
MUTE_ESCALATE_H = 2.0   # default escalate_after_h; 0 never escalates (needs a `why` naming who handles it)
MUTE_ESCALATE_MIN_H = 0.5
# A device hold never stays silent long: a mute whose match (or a condition it covers) names a hold asks
# after at most this long, whatever escalate_after_h says (0 included). A hold with no tenant blocks work.
# screen.hold_mute_escalate_h overrides it, within MUTE_ESCALATE_MIN_H..MUTE_HOLD_ESCALATE_MAX_H (hold_cap).
MUTE_HOLD_ESCALATE_H = 1.0
MUTE_HOLD_ESCALATE_MAX_H = 6.0
MUTE_HOLD_RE = re.compile(r"\b(hold|holds|held|holder|holding)\b", re.I)
# A muted condition not seen for this long has cleared: its next sighting starts a new clock. The
# fallback for runs that never settle (failed or killed ones, sources other than command watchers).
MUTE_CLEAR_GAP_S = 3 * 3600
MUTE_CAP_S = 7 * 86400   # the longest any mute lasts, from its first `since`
MUTE_PROBE_CHARS = 1000
MUTE_CONDS_MAX = 50   # conditions tracked per mute; the longest unseen go first
MUTE_PERSISTS = "under mute: is the expected recovery happening?"   # the escalation's text; effort trigger


def mute(db: DB, source: Any, match: Any, hours: Any, below: Any = None, why: Any = "",
         now: float | None = None, escalate_after_h: Any = None, end_when: Any = None,
         hold_cap_h: float | None = None, throughput: dict | None = None) -> dict:
    """Mute observations from `source` whose text contains `match` (any case) and whose severity is
    below `below` (default critical), for `hours`, or until probe `end_when` passes (`hours` is then
    optional), never past MUTE_CAP_S from its start. A matching condition still seen `escalate_after_h`
    (default MUTE_ESCALATE_H) after it was first muted queues one high event; 0 never does and needs
    a `why`. Muting the same source and match again replaces its end, threshold, reason, escalation
    and probe (the last two kept when not given) and keeps its start, its count and the clocks of its
    conditions, except those that already escalated, which re-arm from now. Raises ValueError on bad
    input. `throughput` (throughmute.vet: a throughput-class mute's owner and max_h) caps its life at
    max_h from now and is kept on it."""
    now = time.time() if now is None else now
    source, match = str(source or "").strip(), str(match or "").strip()
    why = str(why or "").strip()[:300]
    if not source:
        raise ValueError("observation_mute needs `source`, the observations' source as the digest shows it "
                         "(e.g. watcher:<name>)")
    if len(match) < MUTE_MIN_MATCH:
        raise ValueError(f"observation_mute needs `match`, a piece of the observation's text of at least "
                         f"{MUTE_MIN_MATCH} characters; got {match!r}")
    probe = end_when.strip() if isinstance(end_when, str) else end_when
    if probe and (not isinstance(probe, str) or "\n" in probe or len(probe) > MUTE_PROBE_CHARS):
        raise ValueError(f"observation_mute `end_when` must be one shell command on one line, at most "
                         f"{MUTE_PROBE_CHARS} characters (exit 0 once the mute can end, 1 while not)")
    if probe:
        from .ends import check_not_trivial, check_sh
        check_sh(probe, "observation_mute `end_when`")
        check_not_trivial(probe, "observation_mute `end_when`")
    h = None
    if not (probe and hours in (None, "")):
        try:
            h = float(hours)
        except (TypeError, ValueError):
            h = float("nan")
        if not 1 <= h <= MUTE_MAX_H:
            raise ValueError(f"observation_mute needs `hours` from 1 to {MUTE_MAX_H} (optional with an "
                             f"`end_when` probe); got {hours!r}")
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
    cap_h = _hold_cap_h({"hold_cap_h": hold_cap_h})   # hold_cap(cfg) from the caller, else the default
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        old = next((m for m in active if _same_mute(m, source, match)), None)
        # Only mutes still in force hold a slot: an ended one waits for its summary, not for a slot.
        if old is None and sum(float(m["until"]) > now for m in active) >= MUTE_MAX:
            raise ValueError(f"observation_mute rejected: {MUTE_MAX} mutes are already active; let one end first")
        if esc is None:
            esc = float(old.get("escalate_after_h", MUTE_ESCALATE_H)) if old else MUTE_ESCALATE_H
        held = holds_mute(match, old)
        if held and not 0 < esc <= cap_h:
            esc = cap_h
        probe = probe or (old or {}).get("end_when") or None
        if probe is None and h is None:
            raise ValueError(f"observation_mute needs `hours` from 1 to {MUTE_MAX_H} or an `end_when` probe")
        since = float(old["since"]) if old else now
        cap = since + MUTE_CAP_S
        if cap <= now + 60:
            raise ValueError(f"observation_mute rejected: {source} {match!r} has been muted for "
                             f"{MUTE_CAP_S // 86400} days, the most a mute lasts; let it end and check why its "
                             f"condition still recurs before muting it again")
        until = min(cap, now + (h * 3600 if h is not None else MUTE_CAP_S))
        if throughput:
            until = min(until, now + float(throughput["max_h"]) * 3600)
        conds = {k: ({**c, "first_at": now, "escalated": False} if c.get("escalated") else c)
                 for k, c in ((old or {}).get("conds") or {}).items()}
        m = {"source": source, "match": match, "below": below, "hours": h, "why": why,
             "since": since, "until": until, "capped": until >= cap,
             "count": old["count"] if old else 0, "last_at": old["last_at"] if old else None,
             "escalate_after_h": esc, "armed_at": now, "conds": conds}
        m["hold_cap_h"] = cap_h
        for k in ("first_seen", "hourly"):   # throughmute's counts outlive a renewal
            if (old or {}).get(k):
                m[k] = old[k]
        if throughput:
            m["throughput"] = throughput
        if held:
            m["hold"] = True
        if probe:
            m["end_when"] = probe
            if old and old.get("end_when") == probe and old.get("probe_broken"):
                m["probe_broken"] = old["probe_broken"]   # reported once per probe, not again on each extension
        db.set_kv(MUTES_KEY, [x for x in active if x is not old] + [m])
    return m


def hold_cap(cfg: dict | None) -> float:
    """The hold-mute cap in hours: screen.hold_mute_escalate_h, else MUTE_HOLD_ESCALATE_H, kept within
    MUTE_ESCALATE_MIN_H..MUTE_HOLD_ESCALATE_MAX_H."""
    try:
        h = float(((cfg or {}).get("screen") or {}).get("hold_mute_escalate_h") or MUTE_HOLD_ESCALATE_H)
    except (TypeError, ValueError):
        h = MUTE_HOLD_ESCALATE_H
    return min(MUTE_HOLD_ESCALATE_MAX_H, max(MUTE_ESCALATE_MIN_H, h))


def _hold_cap_h(m: dict) -> float:
    try:
        return min(MUTE_HOLD_ESCALATE_MAX_H, float(m.get("hold_cap_h") or MUTE_HOLD_ESCALATE_H))
    except (TypeError, ValueError):
        return MUTE_HOLD_ESCALATE_H


def holds_mute(match: str, m: dict | None = None) -> bool:
    """Whether a mute of `match` (or a condition mute `m` already covers) names a device hold."""
    texts = [match] + [str(c.get("text") or "") for c in ((m or {}).get("conds") or {}).values()]
    return any(MUTE_HOLD_RE.search(t) for t in texts)


def hold_cap_note(m: dict, asked: Any) -> str:
    """The ack of a hold mute whose escalate_after_h was capped (MUTE_HOLD_ESCALATE_H), else ''."""
    try:
        want = float(asked)
    except (TypeError, ValueError):
        want = None if asked in (None, "") else -1.0
    if not m.get("hold") or want is None or want == float(m["escalate_after_h"]):
        return ""
    return (f"observation_mute {m['source']} {m['match']!r}: it covers a device hold, so it asks after "
            f"{_h(float(m['escalate_after_h']) * 3600)} h, not {asked!r} (a hold is never muted longer)")


def _same_mute(m: dict, source: str, match: str) -> bool:
    return m["source"].lower() == source.lower() and m["match"].lower() == match.lower()


def mutes(db: DB, now: float | None = None) -> list[dict]:
    """The mutes still in force."""
    now = time.time() if now is None else now
    return [m for m in db.kv(MUTES_KEY, []) or [] if float(m["until"]) > now]


def slots_line(muted: list[dict], now: float | None = None) -> str:
    """'; 12 of 20 mute slots free' (or '; no mute slot free (all 20 used)'), with the mute that ends
    first once MUTE_SLOTS_NEAR or fewer are free, so a turn does not send a mute the cap refuses."""
    free = max(0, MUTE_MAX - len(muted))
    head = f"; {free} of {MUTE_MAX} mute slots free" if free else f"; no mute slot free (all {MUTE_MAX} used)"
    if free > MUTE_SLOTS_NEAR or not muted:
        return head
    now = time.time() if now is None else now
    first = min(muted, key=lambda m: float(m["until"]))
    return (f"{head}, first to end: {first['source']} "
            f"{clip_match(first['match'])!r} in {max(0.0, (float(first['until']) - now) / 3600):.1f} h")


def clip_match(match: str, n: int = 40) -> str:
    return match if len(match) <= n else match[: n - 1] + "…"


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
                throughmute.tally(m, now)
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
    cap_h = _hold_cap_h(m)
    named = bool(key and MUTE_HOLD_RE.search(key.replace("_", " ").replace("-", " ")))   # the watcher's own key
    for key, item, cleared in _mute_conditions(m, source, text, whole, key):
        held = named or MUTE_HOLD_RE.search(item)
        esc_c = cap_h if held and not 0 < esc_h <= cap_h else esc_h
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
        if esc_c > 0 and not c["escalated"] and age >= esc_c * 3600:
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


def expire_mutes(db: DB, now: float | None = None, where: Any = None) -> list[dict]:
    """End the mutes whose time is up, recording one summary observation event for each (queued only
    when observations were muted). Clock times are in the zone of `where` (the project; None is UTC)."""
    now = time.time() if now is None else now
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        done = [m for m in active if float(m["until"]) <= now]
        if not done:
            return []
        db.set_kv(MUTES_KEY, [m for m in active if float(m["until"]) > now])
        for m in done:
            _ended(db, m, min(now, float(m["until"])), now,
                   f"it reached the {MUTE_CAP_S // 86400}-day cap on a mute: if its condition still recurs, "
                   f"check why before muting it again" if m.get("capped") else "", where)
    return done


def end_mute(db: DB, source: str, match: str, probe: str, why: str, now: float | None = None,
             where: Any = None) -> dict | None:
    """End the active mute of `source` and `match` whose `end_when` is still `probe` (it passed),
    recording its one summary event with `why` as handled; None when no such mute is left."""
    now = time.time() if now is None else now
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        m = next((x for x in active if _same_mute(x, source, match) and x.get("end_when") == probe
                  and float(x["until"]) > now), None)
        if m is None:
            return None
        db.set_kv(MUTES_KEY, [x for x in active if x is not m])
        _ended(db, m, now, now, why, where, settled=True)
    return m


def end_mute_by(db: DB, source: str, match: str, who: str, now: float | None = None,
                where: Any = None) -> dict | None:
    """End the active mute of `source` and `match` at someone's word (`ttp mute end`), recording `who`
    ended it, and queue its one summary event; None when no such mute is active."""
    now = time.time() if now is None else now
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        m = next((x for x in active if _same_mute(x, source, match) and float(x["until"]) > now), None)
        if m is None:
            return None
        db.set_kv(MUTES_KEY, [x for x in active if x is not m])
        m["ended_by"] = who
        _ended(db, m, now, now, f"ended by {who}", where)
    return m


def schedule_of(source: Any) -> str | None:
    """The schedule a command watcher's mute source (watcher:<name> or watcher-error:<name>) belongs
    to; None for any other source."""
    kind, _, name = str(source or "").partition(":")
    return name if kind in ("watcher", "watcher-error") and name else None


def end_schedule_mutes(db: DB, names: Any, why: str, now: float | None = None,
                       where: Any = None) -> list[dict]:
    """End the active mutes of the schedules `names` (turned off or removed: they observe nothing, so
    their mutes mute nothing), recording each one's summary event with `why` as handled."""
    now = time.time() if now is None else now
    names = {str(n) for n in names}
    if not names:
        return []
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        done = [m for m in active if float(m["until"]) > now and schedule_of(m["source"]) in names]
        if not done:
            return []
        db.set_kv(MUTES_KEY, [m for m in active if not any(m is d for d in done)])
        for m in done:
            _ended(db, m, now, now, why, where, settled=True)
    return done


TASK_MUTE_RE = re.compile(r"(?:probe:)?#(\d+)", re.I | re.A)   # a mute keyed to one task: its whole match


def task_of(match: Any) -> int | None:
    """The task a mute's whole `match` names ('#<id>' or 'probe:#<id>'); None for any other text."""
    hit = TASK_MUTE_RE.fullmatch(str(match or "").strip())
    return int(hit.group(1)) if hit else None


def end_task_mutes(db: DB, now: float | None = None, where: Any = None) -> list[dict]:
    """End the active mutes keyed to one task (any source) whose task finished (done, failed or
    cancelled), recording each one's summary event with 'task #<id> ended' as handled: the condition
    they muted went with the task. Reads task states, so every way a task finishes is covered."""
    from .db import TERMINAL_TASK_STATES
    now = time.time() if now is None else now
    if not db.kv(MUTES_KEY):
        return []
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        keyed = {id(m): t for m in active if float(m["until"]) > now and (t := task_of(m["match"])) is not None}
        if not keyed:
            return []
        ids = sorted(set(keyed.values()))
        ended = {r["id"] for r in db.q(f"SELECT id FROM tasks WHERE id IN ({','.join('?' * len(ids))}) AND status IN "
                                       f"({','.join('?' * len(TERMINAL_TASK_STATES))})",
                                       (*ids, *TERMINAL_TASK_STATES))}
        done = [m for m in active if keyed.get(id(m)) in ended]
        if not done:
            return []
        db.set_kv(MUTES_KEY, [m for m in active if not any(m is d for d in done)])
        for m in done:
            _ended(db, m, now, now, f"task #{keyed[id(m)]} ended", where, settled=True)
    return done


def off_schedule_mute(db: DB, m: dict) -> bool:
    """Whether mute `m` belongs to a schedule that is off or gone, so it mutes nothing."""
    name = schedule_of(m.get("source"))
    if name is None:
        return False
    row = db.one("SELECT enabled FROM schedules WHERE name=?", (name,))
    return row is None or not row["enabled"]


def mute_probe_broken(db: DB, source: str, match: str, probe: str, why: str, now: float | None = None,
                      where: Any = None) -> bool:
    """Record that the `end_when` probe of a mute is broken and queue one event saying so, once per
    mute and probe. Returns whether it was reported now."""
    now = time.time() if now is None else now
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        m = next((x for x in active if _same_mute(x, source, match) and x.get("end_when") == probe), None)
        if m is None or m.get("probe_broken"):
            return False
        m["probe_broken"] = why[:300]
        db.set_kv(MUTES_KEY, active)
        db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
             (now, "daemon", "observation", "normal",
              f"The end_when probe `{probe}` of the mute of {m['source']} {m['match']!r} is broken ({why}): "
              f"it would never end the mute, which now ends only at {_when(m['until'], where)}. Mute it again "
              f"with a working `end_when` if it should end on its own.", "queued"))
    return True


def _ended(db: DB, m: dict, end: float, now: float, why: str, where: Any, settled: bool = False) -> None:
    """Record the one summary event of mute `m`, which ended at `end` (`why`, if given). It is queued
    for a turn only when observations were muted; one with none, or `settled` (its end_when probe, the
    recovery condition, passed), is recorded handled: there is nothing to decide."""
    n = int(m["count"])
    seen = (f"{n} observation{'' if n == 1 else 's'}, last at {_when(m['last_at'], where)}" if n
            else "no observations")
    db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
         (now, m["source"], "observation", "info",
          f"mute ended: muted {m['source']} {m['match']!r} below {m['below']} for "
          f"{_h(end - float(m['since']))} h" + (f" ({why})" if why else "") + f": {seen}"
          + (f". Muted because: {m['why']}" if m.get("why") else ""), "handled" if settled or not n else "queued"))


def mute_line(m: dict, now: float | None = None, where: Any = None) -> str:
    """One line for the digest and `ttp status`, its clock time in the zone of `where` (the project)."""
    now = time.time() if now is None else now
    n = int(m["count"])
    seen = f"{n} muted, last at {_when(m['last_at'], where)}" if n else "none muted yet"
    ages = mute_ages(m, now)
    esc = float(m.get("escalate_after_h", MUTE_ESCALATE_H))
    return (f"{m['source']} {m['match']!r} below {m['below']}: {seen}; "
            + (f"persisting {', '.join(ages)}; " if ages else "")
            + (f"asks after {_h(esc * 3600)} h; " if esc > 0 else "never asks; ")
            + f"ends in {max(0.0, (float(m['until']) - now) / 3600):.1f} h" + mute_probe(m)
            + throughmute.rate_text(m, now) + (f" ({m['why']})" if m.get("why") else ""))


def mute_probe(m: dict) -> str:
    """' or when `probe` passes' (and whether it is broken) for a mute with an end_when, else ''."""
    if not m.get("end_when"):
        return ""
    return f" or when `{m['end_when']}` passes" + (" (that probe is broken)" if m.get("probe_broken") else "")


def _h(seconds: float) -> str:
    return f"{seconds / 3600:.1f}".rstrip("0").rstrip(".")


def _when(ts: Any, where: Any = None) -> str:
    return timefmt.long(float(ts), where) if ts else "-"
