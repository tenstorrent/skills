# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Upstream notes: lessons for tt-project's maintainers, collected for the user across projects.

A worker hand-off's follow-ups titled `upstream: ...` are not work for its own project. Each
project's daemon appends them to the user's inbox ~/.tt-project/upstream.jsonl, one JSON line per
note with its project, host, task, title, spec and a fingerprint of title and spec, so the same
note proposed twice (by a retry, or by two projects) is kept once.

A project with `upstream.ingest: true` (off by default) reads the inbox: each note it has not seen
becomes an `upstream_note` event for its coordinator. Its read cursor (a byte offset per inbox, and
the fingerprints it has seen) lives in its own database; it never writes to another project's state.
The inboxes on the machines this user's remote projects run on (`ttp create --host`) are read over
ssh at most once an hour, for at most REMOTE_BUDGET_S per daemon tick: hosts left over when the
time runs out are read first on the next tick, so slow or hung machines never hold up dispatch for
long and every machine is read once per round. The ingesting project marks each inbox it read with
upstream-reader.json, so the other projects' coordinators know someone reads the notes and do not
also pass them on to the user.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from . import project

KV_CURSOR = "upstream_cursor"
LOCAL_EVERY_S = 60          # how often an ingesting project looks at this machine's inbox
REMOTE_EVERY_S = 3600       # and at the inboxes on the machines its remote projects run on
READER_FRESH_S = 2 * 86400  # a reader not seen this long is gone: coordinators pass notes on again
SEEN_KEPT = 5000            # fingerprints a project remembers
TITLE_CHARS, SPEC_CHARS = 300, 4000
REMOTE_TIMEOUT_S = 60       # one machine's read
REMOTE_BUDGET_S = 120       # all remote reads in one tick; a machine is started only if its read fits
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
REMOTE = "~/.tt-project/upstream.jsonl"
LOCAL = "@here"             # the cursor's key for this machine's inbox (never a host name)
_clock = time.monotonic


def path() -> Path:
    return project.HOME_DIR / "upstream.jsonl"


def reader_path() -> Path:
    return project.HOME_DIR / "upstream-reader.json"


def is_note(f) -> bool:
    return isinstance(f, dict) and str(f.get("title") or "").strip().lower().startswith("upstream:")


def fingerprint(title: str, spec: str) -> str:
    norm = " ".join(str(title).lower().split()) + "\n" + " ".join(str(spec).lower().split())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def _lines(raw: bytes) -> tuple[list[dict], int]:
    """The complete lines of `raw` as notes, and how many bytes they took. A torn last line waits."""
    end = raw.rfind(b"\n") + 1
    notes = []
    for ln in raw[:end].splitlines():
        try:
            n = json.loads(ln)
        except ValueError:
            continue
        if isinstance(n, dict) and n.get("fp"):
            notes.append(n)
    return notes, end


def append(name: str, task: int | None, followups) -> int:
    """File a hand-off's upstream notes in this machine's inbox; returns how many were new."""
    notes = [f for f in (followups if isinstance(followups, list) else []) if is_note(f)]
    if not notes:
        return 0
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(path(), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "rb+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)   # several daemons of this user append to the same inbox
        f.seek(0)
        have = {n["fp"] for n in _lines(f.read())[0]}
        out = b""
        for n in notes:
            title, spec = str(n["title"]).strip()[:TITLE_CHARS], str(n.get("spec") or "").strip()[:SPEC_CHARS]
            fp = fingerprint(title, spec)
            if fp in have:
                continue
            have.add(fp)
            out += (json.dumps({"ts": time.time(), "project": name, "host": project.hostname(), "task": task,
                                "title": title, "spec": spec, "fp": fp}, sort_keys=True) + "\n").encode()
        if out:
            f.write(out)
            f.flush()
    return out.count(b"\n")


def remote_hosts() -> list[str]:
    """The other machines this user's projects run on (projects created with --host)."""
    here = project.hostname()
    return sorted({e.get("ssh") or e["host"] for e in project.load_registry().get("projects", {}).values()
                   if isinstance(e, dict) and e.get("host") and e["host"] != here})


def _mark(p: project.Project, now: float) -> dict:
    return {"project": p.name, "host": project.hostname(), "ts": now}


def _read_local(offset: int) -> tuple[bytes, int]:
    """(the inbox from `offset` on, its size)."""
    try:
        with open(path(), "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(offset if offset <= size else 0)
            return f.read(), size
    except FileNotFoundError:
        return b"", 0


def _read_remote(host: str, offset: int, mark: dict) -> tuple[bytes, int] | None:
    """The inbox on `host` from `offset` on and its size, marking it read; None if it cannot be reached."""
    cmd = (f"f={REMOTE}; [ -d ~/.tt-project ] && printf %s {shlex.quote(json.dumps(mark))} > ~/.tt-project/upstream-reader.json; "
           f"if [ -f \"$f\" ]; then wc -c < \"$f\" | tr -d ' '; tail -c +{offset + 1} \"$f\"; else echo 0; fi")
    try:
        # `sh -c` so a login shell that is not POSIX (fish, say) runs it too; no stdin, so ssh never waits on it.
        r = subprocess.run([*SSH, "--", host, f"sh -c {shlex.quote(cmd)}"], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=REMOTE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    head, _, body = r.stdout.partition(b"\n")
    try:
        size = int(head.strip() or 0)
    except ValueError:
        return None
    return body, size


def ingest(p: project.Project, cfg: dict, now: float | None = None, force_remote: bool = False) -> int:
    """For a project with `upstream.ingest` on: new notes in the user's inboxes become events for its
    coordinator. Returns how many. A project without it reads nothing and writes nothing."""
    if not (cfg.get("upstream") or {}).get("ingest"):
        return 0
    now = now or time.time()
    db = p.db
    cur = db.kv(KV_CURSOR) or {}
    offsets: dict = dict(cur.get("offsets") or {})
    seen: list = list(cur.get("seen") or [])
    known = set(seen)
    sources: list[tuple[str, bytes, int]] = []
    raw, size = _read_local(int(offsets.get(LOCAL, 0)))
    sources.append((LOCAL, raw, size))
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    project.write_json(reader_path(), _mark(p, now))
    remote_due = float(cur.get("remote_due") or 0)
    pending: list = list(cur.get("remote_pending") or [])   # hosts of this round not yet read
    if force_remote or (not pending and now >= remote_due):
        pending = remote_hosts()
    if pending:
        hosts, start, tried = set(remote_hosts()), _clock(), 0
        while pending:
            host = pending[0]
            if host in hosts:
                if tried and _clock() - start + REMOTE_TIMEOUT_S > REMOTE_BUDGET_S:
                    break             # out of time this tick: the rest are read first on the next
                tried += 1
                got = _read_remote(host, int(offsets.get(host, 0)), _mark(p, now))
                if got is not None:
                    sources.append((host, *got))
            pending.pop(0)
        if not pending:
            remote_due = now + REMOTE_EVERY_S
    added = 0
    me = (p.name, project.hostname())
    from .coordinator import EVENT_CHARS_BY_KIND
    cap = EVENT_CHARS_BY_KIND["upstream_note"]
    for src, raw, size in sources:
        start = int(offsets.get(src, 0))
        if size < start:      # the inbox was cut or replaced: read it again; fingerprints skip the old notes
            offsets[src] = 0
            continue
        notes, used = _lines(raw)
        offsets[src] = start + used
        for n in notes:
            if n["fp"] in known:
                continue
            known.add(n["fp"])
            seen.append(n["fp"])
            if (n.get("project"), n.get("host")) == me:
                continue      # this project's own notes reached its coordinator with the hand-off
            where = f"{n.get('project', '?')}" + (f" #{n['task']}" if n.get("task") else "") + f" on {n.get('host', '?')}"
            text = f"upstream note from {where}: {n.get('title', '')} — {n.get('spec', '')}"
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (now, "upstream", "upstream_note", "normal", text[:cap], "queued"))
            added += 1
    db.set_kv(KV_CURSOR, {"offsets": offsets, "seen": seen[-SEEN_KEPT:], "remote_due": remote_due,
                            "remote_pending": pending})
    return added


def unread(db) -> int:
    """Upstream notes ingested that the coordinator has not yet read."""
    row = db.one("SELECT COUNT(*) AS n FROM events WHERE kind='upstream_note' AND status='queued'")
    return int(row["n"]) if row else 0


def status_line(db, cfg: dict) -> str:
    if not (cfg.get("upstream") or {}).get("ingest"):
        return ""
    n = unread(db)
    return f"{n} upstream note{'' if n == 1 else 's'} not yet read" if n else ""


def reader(now: float | None = None) -> dict | None:
    """The project that reads this machine's inbox, if one did so recently."""
    try:
        r = json.loads(reader_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(r, dict) or (now or time.time()) - float(r.get("ts") or 0) > READER_FRESH_S:
        return None
    return r


def digest_line(p: project.Project, cfg: dict) -> str:
    """Tells the coordinator, on a turn with upstream notes, whether to pass them on to the user."""
    if (cfg.get("upstream") or {}).get("ingest"):
        return ("## Upstream notes: this project reads the user's upstream inbox; `upstream_note` events are "
                "notes from the user's projects. Do not pass them on to the user.")
    r = reader()
    if r:
        return (f"## Upstream notes: project {r.get('project')} on {r.get('host')} reads the user's upstream inbox, "
                f"which already has this project's `upstream: ...` follow-ups. Do not pass them on to the user.")
    return ("## Upstream notes: no project reads the user's upstream inbox. Pass `upstream: ...` follow-ups on "
            "to the user with a `notify` at severity `low`.")
