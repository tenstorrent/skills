# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The user's machines, and resources that keep failing.

~/.tt-project/machines.json lists the machines this user has, by alias, with tags (what each one
offers, e.g. `device`) and a short note. It is per user, so every project of the user sees the same
list; which of them a project may use is the charter's Resources section. A task that runs on a
machine names its alias in `resources`, so failures can be counted per machine.

A resource is in trouble when the tasks using it keep failing in the last 24 h: runs that crashed,
stalled, timed out or were lost, hand-offs that failed or blocked, host reboots while it was held.
The coordinator routes around it: the tasks move to a machine the charter allows that shares its
tags. Many waits alone also show up, but only as a hint: a busy resource is not a broken one.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import project

ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,79}")   # an alias is also a resource name
TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,39}")
NOTE_CHARS = 200
DIGEST_MACHINES = 20        # machines the digest lists at most
TROUBLE_AT = 2              # failures in 24 h that put a resource in trouble
WAITS_AT = 6                # or this many waits on it (it may be down, not busy)
BAD_RUNS = ("failed", "lost", "timeout", "stalled")


def path() -> Path:
    return project.HOME_DIR / "machines.json"


def load() -> dict[str, dict]:
    """{alias: {"tags": [...], "note": str, "added": ts}}; {} when there is no list yet."""
    try:
        data = json.loads(path().read_text())
    except (FileNotFoundError, ValueError):
        return {}
    got = data.get("machines") if isinstance(data, dict) else None
    return {k: v for k, v in got.items() if isinstance(v, dict)} if isinstance(got, dict) else {}


def _save(machines: dict[str, dict]) -> None:
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    project.write_json(path(), {"machines": machines})
    os.chmod(path(), 0o600)


def tag_list(v: Any) -> list[str]:
    """Tags from "device,x86" or a list; rejects anything that is not a plain word."""
    items = v if isinstance(v, list) else str(v or "").replace(",", " ").split()
    tags = []
    for t in items:
        t = str(t).strip().lower()
        if not TAG_RE.fullmatch(t):
            raise ValueError(f"not a tag: {t!r}")
        if t not in tags:
            tags.append(t)
    return tags


def add(alias: str, tags: Any = None, note: str | None = None) -> dict:
    """Add a machine, or update its tags and note (a note left out keeps the old one)."""
    alias = (alias or "").strip()
    if not ALIAS_RE.fullmatch(alias):
        raise ValueError(f"not a machine alias: {alias!r} (letters, digits and _.@+- only)")
    machines = load()
    old = machines.get(alias) or {}
    entry = {"tags": tag_list(tags) if tags is not None else old.get("tags", []),
             "note": " ".join(str(note).split())[:NOTE_CHARS] if note is not None else old.get("note", ""),
             "added": old.get("added") or time.time()}
    machines[alias] = entry
    _save(machines)
    return entry


def remove(alias: str) -> bool:
    machines = load()
    if machines.pop((alias or "").strip(), None) is None:
        return False
    _save(machines)
    return True


def line(alias: str, m: dict) -> str:
    tags = f" [{', '.join(m.get('tags') or [])}]" if m.get("tags") else ""
    note = f": {m['note']}" if m.get("note") else ""
    return f"{alias}{tags}{note}"


def alternatives(alias: str, machines: dict[str, dict], avoid: set[str]) -> list[str]:
    """Other machines sharing a tag with `alias`, most shared tags first, none in `avoid`."""
    tags = set((machines.get(alias) or {}).get("tags") or [])
    if not tags:
        return []
    scored = [(-len(tags & set(m.get("tags") or [])), a) for a, m in machines.items()
              if a != alias and a not in avoid and tags & set(m.get("tags") or [])]
    return [a for _, a in sorted(scored)]


def _labels(t: dict) -> set[str]:
    from .coordinator import task_resources
    return task_resources(t)


def stats(db, now: float | None = None) -> dict[str, dict]:
    """Every resource with a failure or wait in the last 24 h: {name: counts}."""
    now = now or time.time()
    since = now - 86400
    stats: dict[str, dict] = {}

    def bump(names, key):
        for n in names:
            s = stats.setdefault(n, {"runs": 0, "handoffs": 0, "reboots": 0, "waits": 0})
            s[key] += 1
    for r in db.q("SELECT r.status, r.note, t.labels FROM runs r JOIN tasks t ON t.id=r.task "
                  f"WHERE r.role!='coordinator' AND r.ended>? AND r.status IN ({','.join('?' * len(BAD_RUNS))})",
                  (since, *BAD_RUNS)):
        if r["status"] == "lost" and "lost_to_reboot" in (r["note"] or ""):
            continue   # counted once, as the reboot below
        bump(_labels(r), "runs")
    # Only what the task's own runs reported: a block the daemon set on a dead dependency says
    # nothing about the resource.
    for e in db.q("SELECT e.kind, t.labels FROM events e JOIN tasks t ON t.id=e.task "
                  "WHERE e.ts>? AND e.source LIKE 'task:%' "
                  "AND e.kind IN ('task_failed','task_blocked','task_waiting')", (since,)):
        bump(_labels(e), "waits" if e["kind"] == "task_waiting" else "handoffs")
    for b in db.boots(since):
        bump({str(h).split(":", 1)[0] for h in b.get("held") or [] if ":" in str(h)}, "reboots")
    for s in stats.values():
        s["failures"] = s["runs"] + s["handoffs"] + s["reboots"]
    return stats


def trouble(db, now: float | None = None, seen: dict[str, dict] | None = None) -> dict[str, dict]:
    """Resources whose tasks keep failing in the last 24 h: {name: counts, open task ids}.
    `seen` is stats() already read for the same moment."""
    seen = stats(db, now) if seen is None else seen
    out = {name: dict(s) for name, s in seen.items() if s["failures"] >= TROUBLE_AT or s["waits"] >= WAITS_AT}
    if out:
        for t in db.q("SELECT id, labels FROM tasks WHERE status NOT IN ('done','failed','cancelled') ORDER BY id"):
            for name in _labels(t) & set(out):
                out[name].setdefault("tasks", []).append(t["id"])
    return out


def trouble_line(name: str, s: dict, machines: dict[str, dict], avoid: set[str]) -> str:
    parts = [f"{s[k]} {label}" for k, label in (("runs", "runs crashed, stalled or lost"),
                                                ("handoffs", "hand-offs failed or blocked"),
                                                ("reboots", "host reboots while held"),
                                                ("waits", "waits")) if s.get(k)]
    tasks = s.get("tasks") or []
    on = f"; open tasks on it: {', '.join(f'#{t}' for t in tasks[:10])}" if tasks else "; no open tasks on it"
    if name not in machines:
        alt = "; not in the machines list, so no alternative is known"
    else:
        alts = alternatives(name, machines, avoid)
        alt = (f"; machines sharing its tags: {', '.join(alts)}" if alts
               else "; no other machine shares its tags")
    hint = "; waits only: it may be busy, not down, so do not pause it for this" if not s.get("failures") else ""
    return f"{name}: {', '.join(parts)}{on}{alt}{hint}"


def digest_lines(db, paused: dict | None = None, now: float | None = None) -> list[str]:
    """The Machines and Resource trouble sections of the coordinator's digest; [] when both are empty."""
    machines, lines = load(), []
    if machines:
        lines.append("## Machines (the user's list; use only those the charter's Resources allows; "
                     "a task on one names its alias in `resources`)")
        for alias in sorted(machines)[:DIGEST_MACHINES]:
            lines.append(f"- {line(alias, machines[alias])}")
        if len(machines) > DIGEST_MACHINES:
            lines.append(f"- … and {len(machines) - DIGEST_MACHINES} more (`ttp machines list`)")
    bad = trouble(db, now)
    if bad:
        avoid = set(bad) | set(paused or {})
        lines.append("## Resource trouble (last 24 h; move its tasks to an allowed healthy alternative)")
        for name in sorted(bad):
            lines.append(f"- {trouble_line(name, bad[name], machines, avoid)}")
    return lines
