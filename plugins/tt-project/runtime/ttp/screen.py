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

from .db import DB, SEVERITY_RANK

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


def screen(db: DB, cfg: dict, source: str, text: str, hint: str | None = None, jev=None,
           rewake_after_s: float | None = None, repeat: bool = False) -> Verdict:
    """Record the observation as an issue and say whether the coordinator should wake for it.

    A known open issue wakes again when `repeat` is set (the watcher says each report is a new
    event), or when it was last seen more than `rewake_after_s` ago (it came back after a quiet
    spell). Without either, a known open issue stays quiet."""
    fp = fingerprint(source, text)
    now = time.time()
    floor = SEVERITY_RANK.get(cfg.get("screen", {}).get("wake_min_severity", "normal"), 1)
    row = db.one("SELECT * FROM issues WHERE fingerprint=?", (fp,))
    if row:
        db.x("UPDATE issues SET last_seen=?, count=count+1 WHERE id=?", (now, row["id"]))
        if row["status"] == "fixed":
            db.x("UPDATE issues SET status='open' WHERE id=?", (row["id"],))
            return Verdict(True, row["severity"], "regressed after fix", fp, row["id"], "dedupe")
        reason = "known issue"
        if row["status"] == "open" and SEVERITY_RANK.get(row["severity"], 1) >= floor:
            if repeat:
                reason = "repeated"
            elif rewake_after_s is not None and now - float(row["last_seen"] or 0) > rewake_after_s:
                reason = f"back after {(now - float(row['last_seen'] or 0)) / 3600:.1f} h quiet"
        return Verdict(reason != "known issue", row["severity"], reason, fp, row["id"], "dedupe")

    severity = hint or rule_severity(text)
    verdict_src, reason = "rules", f"rule severity {severity}"
    if jev is not None and jev.enabled():
        try:
            ans = jev.decide(state=f"source: {source}\nobservation:\n{text[:6000]}", questions={
                "actionable": {"type": "noul",
                               "instructions": "Does this observation describe a problem someone should act on?",
                               "criteria": {"true": "A failure, regression, hang, error or request needing action.",
                                            "false": "Routine output, progress, noise, or an already-resolved state."}},
                "severity": {"type": "score", "instructions": "How severe is it for the project?",
                             "criteria": ["informational", "minor", "significant", "critical outage or data loss"]},
            }, purpose="screen")
            if ans:
                p = float(ans.get("actionable", {}).get("noul", 0.5))
                s = float(ans.get("severity", {}).get("score", 1.0))
                severity = ["info", "normal", "high", "critical"][max(0, min(3, round(s)))]
                if p < 0.35:
                    severity = "info"
                verdict_src, reason = "jev", f"jev actionable={p:.2f} severity={s:.2f}"
        except Exception as e:  # screening must never take the daemon down
            reason += f" (jev unavailable: {type(e).__name__})"

    title = text.strip().splitlines()[0][:160] if text.strip() else source
    issue_id = db.x("INSERT INTO issues(fingerprint,source,first_seen,last_seen,count,title,severity,status,screen) "
                    "VALUES(?,?,?,?,1,?,?,?,?)", (fp, source, now, now, title, severity,
                                                   "open" if severity != "info" else "ignored",
                                                   json.dumps({"by": verdict_src, "reason": reason})))
    return Verdict(SEVERITY_RANK.get(severity, 1) >= floor, severity, reason, fp, issue_id, verdict_src)
