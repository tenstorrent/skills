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

An entry may also carry `min_free_gb`, the disk guard's threshold on that machine's filesystem: it
overrides the project's `disk.min_free_gb` there (0 turns the guard off on it), so a shared disk that
other services keep near full by design does not hold every project. The entry for the machine a
daemon runs on is the one whose alias, or `hostname`, is this machine's short host name.

A project created with --host runs its daemon on that machine, which reads its own copy of the list.
`push` copies the list there, merged alias by alias with what is there: the newest change wins, a
removal included, so a list edited on that machine is never overwritten by an older one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
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


def _doc() -> dict:
    try:
        data = json.loads(path().read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _part(doc: dict, key: str) -> dict:
    got = doc.get(key)
    return got if isinstance(got, dict) else {}


def load() -> dict[str, dict]:
    """{alias: {"tags": [...], "note": str, "added": ts, "updated": ts}}; {} when there is no list yet."""
    return {k: v for k, v in _part(_doc(), "machines").items() if isinstance(v, dict)}


def _save(machines: dict[str, dict], removed: dict[str, float]) -> None:
    """`removed` keeps when each alias was removed, so copies elsewhere learn of the removal."""
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    project.write_json(path(), {"machines": machines, **({"removed": removed} if removed else {})})
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


def gb_value(v: Any) -> float | None:
    """A disk threshold in GB from text or a number; "" or "none" means none (the project's own)."""
    if v is None or str(v).strip().lower() in ("", "none", "default"):
        return None
    try:
        gb = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"not a size in GB: {v!r}") from None
    if not gb >= 0 or gb == float("inf"):
        raise ValueError(f"not a size in GB: {v!r}")
    return gb


def tag_names(v: Any) -> list[str]:
    """Resource names from "a,b" or a list, in order, each once."""
    items = v if isinstance(v, list) else str(v or "").replace(",", " ").split()
    out = []
    for n in items:
        n = str(n).strip()
        if not ALIAS_RE.fullmatch(n):
            raise ValueError(f"not a resource name: {n!r}")
        if n not in out:
            out.append(n)
    return out


def add(alias: str, tags: Any = None, note: str | None = None, min_free_gb: Any = ...,
        hostname: str | None = None, shared: Any = None, until: str | None = None) -> dict:
    """Add a machine, or update its tags, note, disk threshold or host name (one left out keeps the
    old value; a threshold or host name of "" removes it). `shared` names the resources on it that
    all of the user's projects share (shared.py): a list or "a,b"; "" the alias itself; False none.
    Left out, it keeps the old ones. `until` (a delay such as 3d or an ISO time) is when the note
    stops being true; past it the digest marks the note stale. A new note without one has none;
    "" removes it."""
    alias = (alias or "").strip()
    if not ALIAS_RE.fullmatch(alias):
        raise ValueError(f"not a machine alias: {alias!r} (letters, digits and _.@+- only)")
    doc = _doc()
    machines, removed = load(), dict(_part(doc, "removed"))
    old = machines.get(alias) or {}
    now = time.time()
    entry = {"tags": tag_list(tags) if tags is not None else old.get("tags", []),
             "note": " ".join(str(note).split())[:NOTE_CHARS] if note is not None else old.get("note", ""),
             "added": old.get("added") or now, "updated": now}
    if until:
        from .ends import parse_expires
        entry["note_until"] = parse_expires(until, now, what="until")
    elif until is None and note is None and old.get("note_until"):
        entry["note_until"] = old["note_until"]
    gb = old.get("min_free_gb") if min_free_gb is ... else gb_value(min_free_gb)
    if gb is not None:
        entry["min_free_gb"] = gb
    host = old.get("hostname") if hostname is None else hostname.strip()
    if host:
        if not ALIAS_RE.fullmatch(host):
            raise ValueError(f"not a host name: {host!r}")
        entry["hostname"] = host
    if shared is None:
        shared = old.get("shared") or False
    elif shared is not False:
        shared = [alias] if shared == "" else tag_names(shared)
    from . import shared as sh
    sh.check_unshare(old.get("shared"), shared, alias)
    if shared:
        entry["shared"] = shared
    for k in RECOVERY:
        if old.get(k):
            entry[k] = old[k]
    machines[alias] = entry
    removed.pop(alias, None)
    _save(machines, removed)
    return entry


def remove(alias: str) -> bool:
    alias = (alias or "").strip()
    machines, removed = load(), dict(_part(_doc(), "removed"))
    if alias not in machines:
        return False
    from . import shared as sh
    sh.check_unshare(machines[alias].get("shared"), False, alias)
    machines.pop(alias)
    removed[alias] = time.time()
    _save(machines, removed)
    return True


def _when(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def merge(mine: dict, theirs: dict) -> dict:
    """Two copies of the list as one, alias by alias: the newest change wins, a removal included.
    On a tie a machine beats a removal (nothing is lost) and `mine` beats `theirs`."""
    machines, removed = {}, {}
    ours = (_part(mine, "machines"), _part(mine, "removed"))
    other = (_part(theirs, "machines"), _part(theirs, "removed"))
    for alias in set().union(*ours, *other):
        best = None
        for rank, (ms, rs) in ((1, ours), (0, other)):
            m = ms.get(alias)
            if isinstance(m, dict):
                best = max(best or (), (_when(m.get("updated") or m.get("added")), 1, rank, m))
            if alias in rs:
                best = max(best or (), (_when(rs[alias]), 0, rank, rs[alias]))
        if best and best[1]:
            machines[alias] = best[3]
        elif best:
            removed[alias] = best[0]
    return {"machines": machines, **({"removed": removed} if removed else {})}


SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
REMOTE = "~/.tt-project/machines.json"
# Replaces the remote list only if it is still the one read (sha256 of its text), mode 0600.
_WRITE = ("import hashlib,json,os,sys;h=os.path.expanduser('~/.tt-project');os.makedirs(h,mode=0o700,exist_ok=True);"
          "p=os.path.join(h,'machines.json');d=json.load(sys.stdin)\n"
          "try: cur=open(p,'rb').read()\nexcept FileNotFoundError: cur=b''\n"
          "if hashlib.sha256(cur).hexdigest()!=d['expect']: sys.exit(3)\n"
          "fd=os.open(p+'.tmp',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);"
          "os.write(fd,(json.dumps(d['doc'],indent=2,sort_keys=True)+'\\n').encode());os.close(fd);"
          "os.chmod(p+'.tmp',0o600);os.replace(p+'.tmp',p)")


def push(host: str, tries: int = 3) -> str:
    """Copy this user's machines list to another machine (same user), merged with the list there.
    A list there that cannot be read is left alone."""
    mine = _doc()
    if not _part(mine, "machines") and not _part(mine, "removed"):
        return "no machines list to copy"
    for _ in range(tries):
        r = subprocess.run([*SSH, host, f"cat {REMOTE} 2>/dev/null || true"], capture_output=True)
        if r.returncode != 0:
            return f"could not copy the machines list to {host}: {r.stderr.decode(errors='replace').strip()[-200:]}"
        try:
            theirs = json.loads(r.stdout) if r.stdout.strip() else {}
        except ValueError:
            theirs = None
        if not isinstance(theirs, dict):
            return f"left the machines list on {host} alone: {REMOTE} there is not a valid list"
        doc = merge(mine, theirs)
        if doc == merge(theirs, {}):
            return f"machines list on {host} is up to date ({len(doc['machines'])} machines)"
        w = subprocess.run([*SSH, host, f"python3 -c {shlex.quote(_WRITE)}"], text=True, capture_output=True,
                           input=json.dumps({"expect": hashlib.sha256(r.stdout).hexdigest(), "doc": doc}))
        if w.returncode == 0:
            return f"copied the machines list ({len(doc['machines'])} machines) to {host}"
        if w.returncode != 3:
            return f"could not copy the machines list to {host}: {w.stderr.strip()[-200:]}"
    return f"could not copy the machines list to {host}: it kept changing there; try `ttp machines push` again"


def here(known: dict[str, dict] | None = None) -> tuple[str, dict] | None:
    """(alias, entry) for the machine this runs on: its alias or `hostname` is this machine's short
    host name (any case); None when the list has no such entry."""
    me = project.hostname().lower()
    known = load() if known is None else known
    for alias in sorted(known):
        m = known[alias]
        if me in (alias.lower(), str(m.get("hostname") or "").lower()):
            return alias, m
    return None


def disk_min_free_gb(known: dict[str, dict] | None = None) -> tuple[str, float] | None:
    """(alias, GB) when this machine's entry sets its own disk guard threshold, else None."""
    hit = here(known)
    if not hit:
        return None
    try:
        gb = gb_value(hit[1].get("min_free_gb"))
    except ValueError:
        return None
    return (hit[0], gb) if gb is not None else None


RECOVERY = ("recovery_owner", "recovery_fallback")
PROJECT_RE = re.compile(r"[\w.-]{1,100}")


def set_recovery(alias: str, owner: str | None = None, fallback: str | None = None) -> dict:
    """Name the project that recovers machine `alias` when it stays down or held, and the one that
    takes over when the owner cannot act (machine_ledger.py). None keeps a value, "" removes it."""
    doc = _doc()
    machines, removed = load(), dict(_part(doc, "removed"))
    if alias not in machines:
        raise ValueError(f"no machine {alias!r} in `ttp machines list`")
    entry = dict(machines[alias])
    for key, name in zip(RECOVERY, (owner, fallback)):
        if name is None:
            continue
        name = name.strip()
        if name and not PROJECT_RE.fullmatch(name):
            raise ValueError(f"not a project name: {name!r}")
        if name:
            entry[key] = name
        else:
            entry.pop(key, None)
    if entry.get("recovery_fallback") and not entry.get("recovery_owner"):
        raise ValueError("a fallback needs an owner: give --owner too")
    entry["updated"] = time.time()
    machines[alias] = entry
    _save(machines, removed)
    return entry


def line(alias: str, m: dict) -> str:
    tags = f" [{', '.join(m.get('tags') or [])}]" if m.get("tags") else ""
    host = f" (host {m['hostname']})" if m.get("hostname") else ""
    disk = f" (disk guard {m['min_free_gb']:g} GB)" if isinstance(m.get("min_free_gb"), (int, float)) else ""
    note = f": {m['note']}" if m.get("note") else ""
    if note and isinstance(m.get("note_until"), (int, float)):
        note += f" (until {_day(m['note_until'])})"
    shared = m.get("shared")
    shared = f" (shared by all projects: {', '.join(shared)})" if isinstance(shared, list) and shared else ""
    owner = (f" (recovery owner {m['recovery_owner']}" + (f", fallback {m['recovery_fallback']}"
             if m.get("recovery_fallback") else "") + ")") if m.get("recovery_owner") else ""
    return f"{alias}{tags}{host}{disk}{shared}{owner}{note}"


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
    # A self-wait (its own checks, jobs, push or planned window) says nothing about the resource either.
    from .unblock import is_self_wait
    for e in db.q("SELECT e.kind, e.data, t.labels FROM events e JOIN tasks t ON t.id=e.task "
                  "WHERE e.ts>? AND e.source LIKE 'task:%' "
                  "AND e.kind IN ('task_failed','task_blocked','task_waiting')", (since,)):
        if e["kind"] == "task_waiting" and is_self_wait(e["data"]):
            continue
        bump(_labels(e), "waits" if e["kind"] == "task_waiting" else "handoffs")
    # Reboots count only against machines on the list (a lock such as push:<branch> is not a
    # machine), and only when most of the host's reboots happened while it was held: a host that
    # reboots on its own says nothing about what it held at the time.
    boots = db.boots(since)
    known = set(load())
    held: dict[str, int] = {}
    for b in boots:
        for n in {str(h).split(":", 1)[0] for h in b.get("held") or [] if ":" in str(h)} & known:
            held[n] = held.get(n, 0) + 1
    bump([n for n, k in held.items() for _ in range(k) if 2 * k > len(boots)], "reboots")
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


def list_lines() -> list[str]:
    """The Machines section of the coordinator's cached prompt; [] when the list is empty."""
    machines, lines = load(), []
    if machines:
        lines.append("## Machines (the user's list; use only those the charter's Resources allows; "
                     "a task on one names its alias in `resources`)")
        for alias in sorted(machines)[:DIGEST_MACHINES]:
            lines.append(f"- {line(alias, machines[alias])}")
        if len(machines) > DIGEST_MACHINES:
            lines.append(f"- … and {len(machines) - DIGEST_MACHINES} more (`ttp machines list`)")
    return lines


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def stale_notes(machines: dict[str, dict], now: float) -> dict[str, float]:
    """{alias: note_until} for the notes whose `until` passed."""
    return {a: float(m["note_until"]) for a, m in machines.items()
            if m.get("note") and isinstance(m.get("note_until"), (int, float)) and m["note_until"] <= now}


def digest_lines(db, paused: dict | None = None, now: float | None = None) -> list[str]:
    """The Resource trouble section of the coordinator's digest, and machine notes past their
    `until`; [] when there is neither."""
    machines, lines = load(), []
    bad = trouble(db, now)
    if bad:
        avoid = set(bad) | set(paused or {})
        lines.append("## Resource trouble (last 24 h; move its tasks to an allowed healthy alternative)")
        for name in sorted(bad):
            lines.append(f"- {trouble_line(name, bad[name], machines, avoid)}")
    stale = stale_notes(machines, time.time() if now is None else now)
    if stale:
        lines.append("## Machine notes past their end (may no longer hold; do not act on them as current)")
        for alias in sorted(stale)[:DIGEST_MACHINES]:
            lines.append(f"- {alias}: {machines[alias]['note']} (stale since {_day(stale[alias])})")
    return lines
