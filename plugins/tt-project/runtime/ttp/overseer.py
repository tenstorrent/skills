# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The overseer: a per-project process beside the daemon that judges the tick loop by its results.

A daemon whose every tick step overruns its budget still refreshes its heartbeat, so "last tick 14 s
ago" reads fine while nothing dispatches, no finished run is reaped and no coordinator answer is
applied. The overseer shares no process, lock or tick step with the daemon. Each pass (EVERY_S) it
opens project.db read-only with a short busy timeout (a WAL reader never waits on the daemon's write
lock), reads the run folders and writes RESULTS to state/HEALTH_FILE:

- last_dispatch: when a worker run last started;
- urgent_wait_s: how long the oldest ready urgent task waits while a worker slot is free;
- unreaped / unreaped_s: finished runs (exit.json on disk) still `running`, and the oldest one's age;
- unapplied / unapplied_s: the same for coordinator runs (an answer finished but not applied);
- undelivered / undelivered_s: events still `new` for the coordinator, and the oldest one's age;
- overruns_h: tick_step_slow reports in the last hour (the daemon raises at most one per step per
  STEP_SLOW_EVERY_S, so this counts the slow steps, a floor on the overruns);
- heartbeat_s: the daemon heartbeat's age.

`line` turns that into one line, shown in every coordinator digest, `ttp status` and `ttp deployed`.
A RESULTS FAILURE is ready urgent work with a free slot, a finished run unreaped or an answer
unapplied for over FAIL_S, or a stale heartbeat. On its first pass in a failure the overseer captures
the daemon's stacks (py-spy dump when installed, else the faulthandler dump the daemon registers on
SIGUSR1) under state/incidents/ and records one incident line (in the health file and appended to
INCIDENTS_FILE); the incident closes when the results are good again. The daemon in turn restarts
an overseer whose heartbeat is stale (Daemon.check_overseer); the overseer's service has Restart=always.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .project import Project, durable_write

EVERY_S = 60            # one pass a minute: a few read-only queries and a stat per running run
FAIL_S = 300            # results failing this long make an incident
STALE_S = 300           # the daemon restarts an overseer whose heartbeat is older than this
BUSY_MS = 2000          # a read that waits longer on the database is skipped, never blocks the loop
HEALTH_FILE = "loop-health.json"
HEARTBEAT_FILE = "overseer.heartbeat"
INCIDENTS_FILE = "overseer-incidents.jsonl"
LOCK_FILE = "overseer.lock"
PYSPY_S = 30


def _ro_db(path: Path):
    """A db.DB over a read-only connection: its query helpers (ready_tasks, kv) without its writes."""
    from . import db as dbm
    conn = sqlite3.connect(f"{Path(path).absolute().as_uri()}?mode=ro", uri=True, timeout=BUSY_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={BUSY_MS}")
    conn.row_factory = sqlite3.Row
    d = dbm.DB.__new__(dbm.DB)
    d.path, d.conn, d._after_commit, d._on_rollback, d.billing_memo, d.ai_tag = str(path), conn, [], [], {}, ""
    return d


def _age(now: float, ts) -> float | None:
    return max(0.0, now - float(ts)) if ts else None


def measure(p: Project, now: float | None = None) -> dict:
    """The loop's results now (module doc), or {"error": ...} when the database cannot be read."""
    from . import urgent
    from .daemon import heartbeat
    now = time.time() if now is None else now
    h: dict = {"at": now}
    hb = heartbeat(p) or {}
    h["heartbeat_s"] = hb.get("age")
    try:
        d = _ro_db(p.state / "project.db")
    except sqlite3.Error as e:
        return {**h, "error": f"database unreadable: {e}"}
    try:
        d.conn.execute("BEGIN")   # one snapshot for every read of this pass
        h["last_dispatch"] = (d.one("SELECT MAX(started) t FROM runs WHERE role!='coordinator'") or {}).get("t")
        running = d.q("SELECT id, role, dir FROM runs WHERE status='running'")
        workers = sum(1 for r in running if r["role"] != "coordinator")
        try:
            slots = int((p.config().get("budget") or {}).get("max_parallel_workers", 6))
        except Exception:
            slots = 6
        h["free_slots"] = max(slots - workers, 0)
        waits = [_age(now, max(float(t["updated"] or t["created"] or now), float(t["not_before"] or 0)))
                 for t in d.ready_tasks() if urgent.is_urgent(t)]
        h["urgent_ready"] = len(waits)
        h["urgent_wait_s"] = max(waits) if waits and h["free_slots"] else 0.0
        done = {}
        for r in running:
            f = Path(r["dir"] or p.runs / str(r["id"])) / "exit.json"
            try:
                done[r["id"]] = (r["role"], f.stat().st_mtime)
            except OSError:
                pass
        coord = [ts for role, ts in done.values() if role == "coordinator"]
        h["unreaped"], h["unreaped_s"] = len(done), max((now - ts for _, ts in done.values()), default=0.0)
        h["unapplied"], h["unapplied_s"] = len(coord), max((now - ts for ts in coord), default=0.0)
        ev = d.one("SELECT COUNT(*) n, MIN(ts) t FROM events WHERE status='new'") or {}
        h["undelivered"], h["undelivered_s"] = int(ev.get("n") or 0), _age(now, ev.get("t")) or 0.0
        h["overruns_h"] = int((d.one("SELECT COUNT(*) n FROM events WHERE kind='tick_step_slow' AND ts>?",
                                     (now - 3600,)) or {}).get("n") or 0)
    except sqlite3.Error as e:
        h["error"] = f"database read failed: {e}"
    finally:
        d.conn.close()
    h["failing"] = failures(h)
    return h


def failures(h: dict) -> list[str]:
    """What makes `h` a results failure (module doc), in words; [] when the loop delivers."""
    from .daemon import HEARTBEAT_STALE_S
    out = []
    if (h.get("urgent_wait_s") or 0) > FAIL_S:
        out.append(f"urgent work ready with a free slot for {_mins(h['urgent_wait_s'])}")
    if (h.get("unreaped_s") or 0) > FAIL_S:
        out.append(f"{h['unreaped']} finished run(s) unreaped for {_mins(h['unreaped_s'])}")
    if (h.get("unapplied_s") or 0) > FAIL_S:
        out.append(f"{h['unapplied']} coordinator answer(s) unapplied for {_mins(h['unapplied_s'])}")
    if h.get("heartbeat_s") is not None and h["heartbeat_s"] > HEARTBEAT_STALE_S:
        out.append(f"loop heartbeat stale for {_mins(h['heartbeat_s'])}")
    return out


def _mins(s: float) -> str:
    return f"{int(s // 60)} min" if s >= 60 else f"{int(s)} s"


def line(h: dict | None, now: float | None = None) -> str:
    """One line of results for the digest, `ttp status` and `ttp deployed`."""
    now = time.time() if now is None else now
    if not h:
        return "loop health: no overseer reading yet"
    if h.get("error"):
        return f"loop health: {h['error']}"
    age = now - float(h.get("at") or 0)
    last = h.get("last_dispatch")
    parts = [f"last dispatch {_mins(now - float(last))} ago" if last else "no dispatch yet",
             f"urgent ready {h.get('urgent_ready', 0)}" + (f" (waiting {_mins(h['urgent_wait_s'])} with a free slot)"
                                                           if h.get("urgent_wait_s") else ""),
             f"unreaped {h.get('unreaped', 0)}", f"unapplied answers {h.get('unapplied', 0)}",
             f"undelivered events {h.get('undelivered', 0)}", f"step overruns {h.get('overruns_h', 0)}/h"]
    text = "loop health: " + ", ".join(parts)
    if h.get("failing"):
        text += "; FAILING: " + "; ".join(h["failing"])
    inc = h.get("incident")
    if inc:
        text += f"; incident since {time.strftime('%H:%M', time.localtime(inc['at']))}, stacks {inc.get('stacks')}"
    if age > STALE_S:
        text += f" (overseer reading {_mins(age)} old)"
    return text


def read(p: Project) -> dict | None:
    try:
        h = json.loads((p.state / HEALTH_FILE).read_text())
        return h if isinstance(h, dict) else None
    except (OSError, ValueError):
        return None


def digest_lines(p: Project, now: float | None = None) -> list[str]:
    return [line(read(p), now)]


def heartbeat_age(p: Project, now: float | None = None) -> float | None:
    try:
        return max(0.0, (time.time() if now is None else now) - (p.state / HEARTBEAT_FILE).stat().st_mtime)
    except OSError:
        return None


def capture_stacks(p: Project, now: float, why: str) -> Path | None:
    """The daemon's stacks as an incident file: py-spy dump when installed, else its faulthandler dump."""
    from .daemon import _is_daemon, _read_pid
    pid = _read_pid(p.state / "daemon.pid")
    if not pid or not _is_daemon(pid):
        return None
    inc = p.state / "incidents"
    inc.mkdir(parents=True, exist_ok=True)
    path = inc / f"loop-results-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}-{pid}.txt"
    spy = shutil.which("py-spy")
    if spy:
        try:
            r = subprocess.run([spy, "dump", "--pid", str(pid)], capture_output=True, text=True, timeout=PYSPY_S)
            if r.returncode == 0 and r.stdout.strip():
                durable_write(path, f"daemon pid {pid} of project {p.name}: {why}\npy-spy dump\n{r.stdout}")
                return path
        except (OSError, subprocess.SubprocessError):
            pass
    from .watchdog import DUMP_WAIT_S, save_dump
    saved = save_dump(p, pid, now, 0, DUMP_WAIT_S)
    saved.replace(path)
    return path


def check(p: Project, now: float | None = None) -> dict:
    """One pass: measure, open or close the incident, write the health file and the heartbeat."""
    now = time.time() if now is None else now
    h = measure(p, now)
    prev = read(p) or {}
    inc = prev.get("incident")
    if h.get("failing") and not inc:
        why = "; ".join(h["failing"])
        try:
            stacks = capture_stacks(p, now, why)
        except Exception as e:   # the incident is recorded even when the stacks cannot be had
            stacks = f"not captured ({type(e).__name__}: {e})"
        inc = {"at": now, "why": why, "stacks": str(stacks) if stacks else "not captured (no live daemon)"}
        with open(p.state / INCIDENTS_FILE, "a") as f:
            f.write(json.dumps(inc) + "\n")
    elif not h.get("failing") and not h.get("error"):
        inc = None
    if inc:
        h["incident"] = inc
    durable_write(p.state / HEALTH_FILE, json.dumps(h, default=str))
    durable_write(p.state / HEARTBEAT_FILE, json.dumps({"pid": os.getpid(), "at": now}))
    return h


def main() -> int:
    p = Project(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    lock = open(p.state / LOCK_FILE, "a")
    try:   # one overseer per project: a second start (the daemon's restart racing systemd's) exits
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    while not stop and (p.state / "project.db").exists():   # a removed project ends it
        try:
            check(p)
        except Exception as e:   # a bad pass is logged; the next one runs
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} overseer: {type(e).__name__}: {e}", flush=True)
        for _ in range(EVERY_S):
            if stop:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
