# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-free warning for a command watcher that opens many issues on one subject.

A watcher whose lines carry changing text (a varying error reason, a duration such as 'for 0.7h', a
long line that splits at '; ') opens a new issue for each variant or fragment. When one watcher source
opens more than `screen.same_subject_warn` issues (default 3; 0 turns it off) of one kind on the same
subject within 24 h, the daemon logs one line and records it for the coordinator's digest and the daily
review. It never wakes anyone and changes no issue: the fix is in the watcher's output (a stable JSON
`key`, or `"whole": true`).

Issues of one kind are duplicates; distinct stable keys are distinct conditions. All issues opened
under no key on a subject are one kind (their text changed), except those opened before issues had
keys (db.WATCH_KEY_SINCE), which are not counted. A keyed issue's kind is its condition
class: the key, normalized as issue text and subjects are (lower-cased, numbers masked), split at ':',
'/' and '#', without the segments that name its subject (and a leading '<subject>' and separator), and
without the trailing segments that hold a number (a value, a count, an episode, a time), each other run
of digits counted as one number. So 'hold:3' and 'hold:4' (or 'retry 1' and 'retry 2') are one kind,
while 'outage ended', 'drop-in', 'recovered' and '<watcher>:<box>:reboot' next to '<watcher>:<box>:fsm'
are distinct. A keyed issue counts once each time it opens: when it is new and each time it reopens
after a close (the reopen churn of one key), so a condition that keeps clearing and coming back warns
too. A new episode of one key (screen._supersede) counts only when the episode before it was still open
when it came, i.e. the watcher starts a new episode on every run while the condition never clears: a
new episode after the previous one closed (cleared, or closed after a quiet run) is a distinct real
occurrence, as references/watchers.md asks watchers to report, and is not churn. The supersede itself is
not a reopen. The warning lists the keys it counted (the first STORED_KEYS are kept), so a reader can tell
duplicates from distinct conditions."""
from __future__ import annotations

import json
import re
import time

from .db import DB, WATCH_KEY_SINCE
from . import screen   # a module, not its names: screen imports this module too

WINDOW_S = 86400.0
DEFAULT = 3
KEY = "watcher_same_subject"   # kv: "<source>\n<subject>" -> {source, subject, n, at, kind, keys}
FIX = 'give its JSON lines a stable "key" (or "whole": true), see references/watchers.md in the tt-project-harness skill'
KEYED_FIX = ('give each condition one stable "key", not one per value or occurrence (a new occurrence of one '
             'condition is a new "episode" of its key), see references/watchers.md in the tt-project-harness skill')
CHURN_FIX = ('it keeps closing and reopening, or starts a new "episode" on every run: report the condition on '
             'every run while it holds and start a new episode only once it cleared and came back, see '
             'references/watchers.md in the tt-project-harness skill')
NO_KEY = ""                    # the kind of every issue opened under no key
SHOWN_KEYS = 6
STORED_KEYS = 20             # keys kept per warning; n counts them all
_SEP = re.compile(r"[:/#]")
_DIGITS = re.compile(r"\d+")
_NUMBER = re.compile(r"\d|<(?:n|hex|ts|dur)>")   # a segment holding a number, raw or masked by normalize


def threshold(cfg: dict) -> int:
    try:
        return int((cfg.get("screen") or {}).get("same_subject_warn", DEFAULT))
    except (TypeError, ValueError):
        return DEFAULT


def key_kind(key: str | None, subject: str = "") -> str:
    """The condition class of a watcher key (see the module doc); NO_KEY for none."""
    k = screen.normalize(key or "")
    if not k:
        return NO_KEY
    subj = screen.normalize(subject or "")   # an issue's subject is stored normalized; the key must match it
    if subj and k.startswith(subj) and len(k) > len(subj) and (_SEP.match(k, len(subj)) or k[len(subj)] == " "):
        k = k[len(subj) + 1:].strip() or k
    parts = [x.strip() for x in _SEP.split(k)]
    parts = [x for x in parts if x and x != subj] or [k]
    while len(parts) > 1 and _NUMBER.search(parts[-1]):
        parts.pop()
    return _DIGITS.sub("<n>", ":".join(parts))


def groups(db: DB, source: str, now: float, window_s: float = WINDOW_S) -> list[tuple[str, str, list[str]]]:
    """(subject, kind, keys) for each kind of issue `source` opened over the last `window_s`: an issue
    under no key once ('') when it is new, a keyed one once per open (new or reopened); no subject is ''."""
    out: dict[tuple[str, str], list[str]] = {}
    since = now - window_s
    try:   # a keyless issue opened before issues had keys says nothing about the watcher's keys
        keyed_since = float(db.meta(WATCH_KEY_SINCE) or 0)
    except ValueError:
        keyed_since = 0.0
    for r in db.q("SELECT id, COALESCE(subject,'') AS subject, COALESCE(watch_key,'') AS k, first_seen, reopens, "
                  "episode FROM issues WHERE source=? AND (first_seen>? OR COALESCE(opened,0)>?) "
                  "ORDER BY first_seen, id", (source, since, since)):
        opens = int(float(r["first_seen"] or 0) > since and not _real_episode(db, source, r))
        if r["k"]:
            opens += sum(1 for t in reopen_times(r["reopens"]) if t > since)
        elif float(r["first_seen"] or 0) < keyed_since:
            continue
        if opens:
            out.setdefault((r["subject"], key_kind(r["k"], r["subject"])), []).extend([r["k"]] * opens)
    return [(subject, kind, keys) for (subject, kind), keys in out.items()]


def _real_episode(db: DB, source: str, r) -> bool:
    """Whether keyed issue `r` is a new episode that came after the key's previous episode closed (a real
    new occurrence, never churn): the issue before it under the key was closed before this one opened. A
    supersede closes a still-open issue at the very time its successor opens (screen._supersede), and
    keeps an earlier close time."""
    if not r["k"] or not r["episode"]:
        return False
    prev = db.one("SELECT episode, closed FROM issues WHERE source=? AND watch_key=? AND id<? "
                  "ORDER BY id DESC LIMIT 1", (source, r["k"], r["id"]))
    return bool(prev and prev["episode"] and prev["closed"] is not None
                and float(prev["closed"]) < float(r["first_seen"] or 0))


def reopen_times(raw) -> list[float]:
    """An issue's `reopens` column: the times it reopened lately (JSON list), [] when unset or bad."""
    try:
        got = json.loads(raw or "[]")
        return [float(t) for t in got] if isinstance(got, list) else []
    except (TypeError, ValueError):
        return []


def reopened(raw, now: float, window_s: float = WINDOW_S) -> str:
    """The `reopens` value after one more reopen at `now`, keeping only the last `window_s`."""
    return json.dumps([t for t in reopen_times(raw) if now - t < window_s] + [now])


def counts(db: DB, source: str, now: float, window_s: float = WINDOW_S) -> list[tuple[str, int]]:
    """(subject, issues of its largest kind) for `source` over the last `window_s`; no subject is ''."""
    best: dict[str, int] = {}
    for subject, _kind, keys in groups(db, source, now, window_s):
        best[subject] = max(best.get(subject, 0), len(keys))
    return sorted(best.items())


def check(db: DB, cfg: dict, sources: list[str], now: float | None = None) -> list[str]:
    """Record each source and subject with more issues of one kind than the threshold; return a log
    line for each one not warned about in the last 24 h. A known one only has its count updated."""
    limit = threshold(cfg)
    if limit <= 0:
        return []
    now = time.time() if now is None else now
    warned = {k: w for k, w in (db.kv(KEY, {}) or {}).items() if now - float(w.get("at") or 0) < WINDOW_S}
    new, changed = [], False
    for source in sources:
        noisy: dict[str, tuple[str, list[str]]] = {}
        for subject, kind, keys in groups(db, source, now):
            if len(keys) > limit and len(keys) > len(noisy.get(subject, ("", []))[1]):
                noisy[subject] = (kind, keys)
        for subject, (kind, all_keys) in noisy.items():
            k, keys = f"{source}\n{subject}", all_keys[:STORED_KEYS]
            if k in warned:
                if warned[k]["n"] != len(all_keys) or warned[k].get("keys") != keys:
                    warned[k].update(n=len(all_keys), kind=kind, keys=keys)
                    changed = True
                continue
            warned[k] = {"source": source, "subject": subject, "n": len(all_keys), "at": now, "kind": kind, "keys": keys}
            new.append(describe(warned[k]))
    if new or changed or len(warned) != len(db.kv(KEY, {}) or {}):
        db.set_kv(KEY, warned)
    return new


def describe(w: dict) -> str:
    subject = f"subject '{w['subject']}'" if w["subject"] else "no subject"
    keys = [k for k in (w.get("keys") or []) if k]
    if keys:
        seen: dict[str, int] = {}
        for k in keys:
            seen[k] = seen.get(k, 0) + 1
        names = [k if n == 1 else f"{k} x{n}" for k, n in seen.items()]
        more = len(names) - SHOWN_KEYS + max(0, int(w.get("n") or 0) - len(keys))   # + issues past STORED_KEYS
        shown = ", ".join(names[:SHOWN_KEYS]) + (f", +{more} more" if more > 0 else "")
        return f"{w['source']} opened {w['n']} issues on {subject} within 24 h under key kind " \
               f"'{w.get('kind') or ''}' (keys: {shown}): {CHURN_FIX if len(seen) == 1 else KEYED_FIX}"
    if "keys" in w:
        how = " under no key (changing text)"
    else:   # recorded before keys were
        how = ""
    return f"{w['source']} opened {w['n']} issues on {subject} within 24 h{how}: {FIX}"


def recent(db: DB, since: float, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return sorted((w for w in (db.kv(KEY, {}) or {}).values()
                   if since < float(w.get("at") or 0) and now - float(w.get("at") or 0) < WINDOW_S),
                  key=lambda w: (w["source"], w["subject"]))


def digest_lines(db: DB, since: float, now: float | None = None) -> list[str]:
    """Warnings raised since the coordinator's last turn: nothing to wake for, a watcher to fix."""
    found = recent(db, since, now)
    if not found:
        return []
    return ["## Noisy command watchers (no wake; fix the watcher's output)"] + [f"- {describe(w)}" for w in found]


def review_lines(db: DB, now: float | None = None) -> list[str]:
    """The daily review's lines: every warning of the last 24 h."""
    now = time.time() if now is None else now
    return [describe(w) for w in recent(db, now - WINDOW_S, now)]
