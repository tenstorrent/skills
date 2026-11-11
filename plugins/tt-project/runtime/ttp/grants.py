# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Granted delivery settings against the config: the daemon checks records, never charter prose.

The coordinator applies a setting the user grants with config_set, in the turn that carries the user's
word (the setter record keeps who set it: configwho). `reconcile` keeps only a model-free check of the
last recorded grant, for the settings only the user grants (push permission, the protected push branch,
auto-merge repositories): a setting whose last recorded setter was the user but whose value in
project.json changed after that record (a hand edit, a revert) is one keyed event (EVENT, fingerprint
per setting and state) for the coordinator, never an ask and never a write. The daemon reads no charter
wording to decide it and never sets a setting itself.

`forbidden` stays for settinghold.setup_question: a charter sentence forbidding pushes to the push branch.
"""
from __future__ import annotations

import hashlib
import re

from . import configwho, settinghold

EVENT = "grant_mismatch"
AUTO_MERGE = "delivery.auto_merge_repos"
PUSH = "delivery.push_allowed"
PROTECTED = "delivery.allow_protected_push_branch"
KEYS = (PUSH, PROTECTED, AUTO_MERGE)
# A plain prohibition of pushing; "never force-push" forbids only rewriting the branch, not pushing to it.
_FORBIDS = re.compile(r"\b(?:never|do not|don't|must not|may not|no)\s+push", re.I)


def _flat(s: str) -> str:
    return " ".join(str(s or "").split())


def forbidden(charter: str, push_branch: str) -> str:
    """A charter sentence forbidding pushes to the push branch (any section), or ""."""
    names = {n for n in (push_branch, push_branch.partition("/")[2]) if n}
    if not names:
        return ""
    target = re.compile(r"\bpush[ \t]+branch\b|(?<![\w/-])(?:" + "|".join(
        re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w/-])", re.I)
    for s in re.split(r"(?<=[.!?])\s+|\n+", charter or ""):
        if _FORBIDS.search(s) and target.search(s) and not settinghold._NO_APPROVAL.search(s) \
                and not settinghold._ONLY_IF.search(s):
            return _flat(s)
    return ""


def _fp(key: str, rec: dict) -> str:
    return "grant:" + hashlib.sha256(f"{key}\n{rec.get('h')}\n{rec.get('ts')}".encode()).hexdigest()[:16]


def reconcile(p, db, cfg: dict, now: float, repo=None) -> list[str]:
    """For each user-granted setting, raise one keyed event when its value changed after the user's last
    recorded set (once per setting and recorded set). Nothing is written or applied; returns []."""
    try:
        raw = p.raw_config()
    except Exception:
        return []
    for key in KEYS:
        rec = configwho.get(db, key)
        if not isinstance(rec, dict) or configwho.role(rec) != "user":
            continue
        now_rec = configwho.lookup(db, key, raw)
        if not isinstance(now_rec, dict) or configwho.role(now_rec) == "user":
            continue   # still as the user's grant left it
        fp = _fp(key, rec)
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            continue
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "harness", EVENT, fp, "normal",
              f"Harness defect: {key} was last set by the user ({configwho.describe(rec)}) but it changed "
              f"since ({_flat(now_rec.get('why'))}). Not an ask and not changed by the daemon: if the user's "
              f"word still stands, restore it with config_set in a turn that carries it; else leave it.",
              "queued"))
    return []
