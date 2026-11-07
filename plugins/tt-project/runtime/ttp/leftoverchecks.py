# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Leftover `ttp checks` runs in the host-wide checks queue (their own processes; checkreap ends what a check run left), found and stopped model-free.

A `ttp checks` started outside a tracked job (a `nohup` in a bench cell, a script's background run) can
outlive the run that started it. Its slot or queue ticket is an OS file lock, so it frees only when the
process ends; meanwhile it waits in, or holds a slot of, the queue every project shares. A worker's kill
guard rightly refuses to stop a process that is not its own, so the daemon's checks-queue upkeep
(Daemon.reap_leftover_checks) and `ttp checks --reap [--dry-run]` do it through this pass instead.

A process is a leftover (find) only when all of these hold:
- it is this OS user's, on Linux, and its command line is a `ttp checks` run;
- it has a file under the checks slot folder open (checkcap.slot_dir: a slot, lane or queue ticket);
- it names no other project (procowner.foreign_env) and holds no other `ttp lock` slot (held_lock: an open
  file in the project's or a shared resource's lock folder);
- neither it nor an ancestor is the caller, the project's daemon or one of its running workers
  (leftoverstop._guarded), and it is not a `ttp detach` job's or a device user (leftoverstop._untouchable);
- its task, when it is this project's, is finished; its TTP_RUN_DIR, when set, is not a live run (the
  folder exists without exit.json); its run row, when this project's, is not running;
- and at least one reason holds: its working folder is deleted; its TTP_RUN_DIR is gone; its run ended
  (exit.json) after it started, at least leftoverstop.GRACE_S ago, and it does not outlive its run by
  design (TTP_DETACHED, locks.OUTLIVES_RUN_ENV); or its task, ended at least GRACE_S ago, is cancelled
  or failed, or done with nobody waiting on its run's checks (jobstop.checks_consumer). A daemon review
  precheck (a task with a non-numeric run id, 't<id>-<head>') is never stopped here: checklane decides
  its lane (checklane.open_precheck).

stop() ends it through leftoverstop.end (SIGTERM, then SIGKILL after leftoverstop.TERM_S; its process
group when it leads one whose members are all safe to stop), removes its queue tickets and logs one
audit line to the project's checks waits log (checkcap.WAITS_LOG). Its slots free as it ends.

Nothing here holds its caller for long: each process's reads (probe) run in a thread the caller leaves
after PROBE_S, and each stop's signalling after its own wait plus PROBE_S (a /proc read can block on a
process stuck in the kernel). Such a process is left for a later pass, reported once (no new attempt
while the thread stays stuck on it). The daemon's step (Daemon.reap_leftover_checks) takes one pid
(pids) at a time through reap_one, so it also keeps its step budget and resumes where it stopped.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import locks, procowner

OUTLIVES = ("TTP_DETACHED", locks.OUTLIVES_RUN_ENV)
PROBES = ("--result", "--settle", "--reap", "--gap")
PROBE_S = 10.0   # one process's reads, or a stop past its own wait: past this it is left for a later pass
_stuck: dict[int, threading.Thread] = {}   # pid -> the probe or stop thread still stuck on it


def _bounded(pid: int, fn, timeout: float):
    """("done", fn()) when it returns within `timeout`; ("timeout", None) when it does not (its thread is
    left to finish on its own); ("stuck", None) while an earlier one for `pid` is still stuck."""
    t = _stuck.get(pid)
    if t is not None:
        if t.is_alive():
            return "stuck", None
        _stuck.pop(pid, None)
    box: dict = {}

    def run() -> None:
        try:
            box["v"] = fn()
        except BaseException as e:   # raised again in the caller
            box["e"] = e
    t = threading.Thread(target=run, name=f"leftoverchecks-{pid}", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        _stuck[pid] = t
        return "timeout", None
    if "e" in box:
        raise box["e"]
    return "done", box.get("v")


def pids() -> list[int]:
    """The process ids under /proc but this one, in order ([] off Linux); a pass's items."""
    for pid in [k for k, t in _stuck.items() if not t.is_alive()]:
        _stuck.pop(pid, None)
    if not os.path.isdir("/proc/self/fd"):
        return []
    me = os.getpid()
    try:
        return sorted(int(n) for n in os.listdir("/proc") if n.isdigit() and int(n) != me)
    except OSError:
        return []


def _argv(pid: int) -> list[str]:
    try:
        return [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1]]
    except OSError:
        return []


def is_checks(argv: list[str]) -> bool:
    """Whether a command line runs `ttp checks` (the launcher, or `python -m ttp`), not one of its probes
    (--result, --settle, --reap)."""
    for i, a in enumerate(argv[:-1]):
        if (os.path.basename(a) == "ttp" or (a == "ttp" and i and argv[i - 1] == "-m")) and argv[i + 1] == "checks":
            rest = argv[i + 2:]
            opts = rest[:rest.index("--")] if "--" in rest else rest
            return not any(x.split("=", 1)[0] in PROBES for x in opts)
    return False


def _fds(pid: int) -> list[str]:
    out = []
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return out
    for fd in names:
        try:
            out.append(os.readlink(f"/proc/{pid}/fd/{fd}"))
        except OSError:
            continue
    return out


def _ppid(pid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def started_at(pid: int) -> float | None:
    """When `pid` started, in epoch seconds (boot time plus its start ticks)."""
    try:
        ticks = int(locks.start_time(pid) or "")
        btime = next(int(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines() if x.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration):
        return None


def _cwd_gone(pid: int) -> bool:
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return False
    return cwd.endswith(" (deleted)") or not os.path.isdir(cwd)


def held_lock(p, pid: int, slots: Path) -> bool:
    """Whether `pid` has a file open in a `ttp lock` folder (the project's or a shared resource's,
    jobstop.lock_dirs), the checks slot folder aside. Its own lifetime locks (a clean checkout's
    cleantree.LOCK, check-runs' checkdirs.LOCK, its logs' .lock files) live elsewhere and do not count."""
    from . import jobstop
    root = os.path.realpath(slots)
    dirs = [d for d in (os.path.realpath(x) for x in jobstop.lock_dirs(p)) if d != root]
    return any(f.startswith(d + os.sep) and not f.startswith(root + os.sep) for f in _fds(pid) for d in dirs)


def _facts(p, pid: int, slots: Path, env: dict) -> dict:
    """Every /proc and file read judge needs about `pid`, none from the database (it runs in a probe thread)."""
    from . import leftoverstop
    chain, a = [], pid
    while a > 1 and a not in chain:
        chain.append(a)
        a = _ppid(a)
    run_dir = env.get("TTP_RUN_DIR") or ""
    out = {"untouchable": leftoverstop._untouchable(p, pid, env), "held_lock": held_lock(p, pid, slots),
           "chain": chain, "cwd_gone": _cwd_gone(pid), "run_dir_is": bool(run_dir) and os.path.isdir(run_dir),
           "ended": None, "began": None}
    if run_dir:
        try:
            out["ended"] = (Path(run_dir) / "exit.json").stat().st_mtime
        except OSError:
            pass
        out["began"] = started_at(pid)
    return out


def judge(p, pid: int, slots: Path, guarded: set[int], env: dict, facts: dict | None = None) -> tuple[str, str]:
    """("leftover" | "keep", why) for this user's `ttp checks` process `pid` that has a file under `slots` open;
    `facts` (_facts) as read already. ("timeout" | "stuck", why) when the reads of who joined its checks
    outlast PROBE_S (_bounded)."""
    from . import leftoverstop
    facts = _facts(p, pid, slots, env) if facts is None else facts
    if procowner.foreign_env(p.base, env):
        return "keep", "another project's"
    if why := facts["untouchable"]:
        return "keep", why
    if facts["held_lock"]:
        return "keep", "it holds a ttp lock"
    for a in facts["chain"]:
        if a in guarded:
            return "keep", f"a live worker run or the caller (pid {a}) claims it"
    own = procowner.owns_env(p.base, env)
    task, run_id = env.get("TTP_TASK") or env.get("TTP_TASK_ID") or "", env.get("TTP_RUN_ID") or ""
    if own and task and run_id and not run_id.isdigit():
        return "keep", "a daemon review precheck: checklane decides its lane"
    status = None
    if own and task.isdigit():
        row = p.db.one("SELECT status, updated FROM tasks WHERE id=?", (int(task),))
        status = row["status"] if row else None
        if status and status not in ("done", "failed", "cancelled"):
            return "keep", f"its task #{task} is {status}"
        if status and time.time() - float(row["updated"] or 0) < leftoverstop.GRACE_S:
            status = None   # just ended: its run's own teardown first
    if own and run_id.isdigit():
        row = p.db.one("SELECT status FROM runs WHERE id=?", (int(run_id),))
        if row and row["status"] == "running":
            return "keep", f"its run {run_id} is running"
    run_dir = env.get("TTP_RUN_DIR") or ""
    if facts["run_dir_is"] and facts["ended"] is None:
        return "keep", f"its run {run_dir} is live"
    if facts["cwd_gone"]:
        return "leftover", "its working folder is deleted"
    if run_dir and not facts["run_dir_is"]:
        return "leftover", "its run folder is gone"
    if run_dir and not any(env.get(k) for k in OUTLIVES):
        ended, began = facts["ended"], facts["began"]
        if ended is not None and began is not None and began < ended and time.time() - ended >= leftoverstop.GRACE_S:
            return "leftover", "its run has ended"
    if status in ("failed", "cancelled") and run_id.isdigit():
        return "leftover", f"its task #{task} is {status}"
    if status == "done" and run_id.isdigit():
        from . import jobstop
        why = jobstop.checks_waiter(p, Path(run_dir)) if run_dir else None
        if run_dir and not why:   # the join records' pids are read in a bounded thread (_bounded)
            state, why = _bounded(pid, lambda: jobstop.checks_joiner(p.state, Path(run_dir)), PROBE_S)
            if state != "done":
                return state, f"reading who joined its checks took over {PROBE_S:.0f} s"
        if not why:
            return "leftover", f"its task #{task} is done"
    return "keep", "no sign it outlived its run"


def _probe(p, pid: int, slots: Path, root: str) -> dict | None:
    """In a probe thread: None unless `pid` runs `ttp checks` with a file under the slot folder open; else
    {env, held, start, facts}."""
    from .lockreap import environ
    if not is_checks(_argv(pid)):
        return None
    held = [f for f in _fds(pid) if f.startswith(root + os.sep)]
    if not held:
        return None
    env = environ(pid)
    return {"env": env, "held": held, "start": locks.start_time(pid), "facts": _facts(p, pid, slots, env)}


def check(p, pid: int, slots: Path, guarded: set[int]) -> tuple[str, dict | None]:
    """("leftover", its find entry), ("keep", None), or ("timeout" | "stuck", None) when its reads outlast
    PROBE_S now or an earlier probe is still stuck on it (_bounded): left for a later pass."""
    try:
        if Path(f"/proc/{pid}").stat().st_uid != os.getuid():
            return "keep", None
    except OSError:
        return "keep", None
    slots = Path(slots)
    state, got = _bounded(pid, lambda: _probe(p, pid, slots, os.path.realpath(slots)), PROBE_S)
    if state != "done":
        return state, None
    if got is None:
        return "keep", None
    env = got["env"]
    verdict, why = judge(p, pid, slots, guarded, env, got["facts"])
    if verdict in ("timeout", "stuck"):
        return verdict, None
    if verdict != "leftover":
        return "keep", None
    return "leftover", {"pid": pid, "start": got["start"], "run_dir": env.get("TTP_RUN_DIR") or "", "why": why,
                        "tickets": [f for f in got["held"] if ".queue" + os.sep in f]}


def find(p, slots: Path | None = None, guarded: set[int] | None = None) -> list[dict]:
    """This user's leftover `ttp checks` processes holding or waiting in the checks queue (module doc):
    [{pid, start, run_dir, why, tickets}]. Read only; [] off Linux. A process whose reads time out is left out."""
    from . import checkcap, leftoverstop
    if not os.path.isdir("/proc/self/fd"):
        return []
    slots = Path(checkcap.slot_dir() if slots is None else slots)
    guarded = leftoverstop._guarded(p) if guarded is None else guarded
    return [f for pid in pids() for verdict, f in [check(p, pid, slots, guarded)] if verdict == "leftover"]


def stop(p, found: dict, guarded: set[int] | None = None, term_s: float | None = None) -> dict | None:
    """Stop one leftover (find) through leftoverstop.end: SIGTERM, then SIGKILL after `term_s` (its whole
    process group when it leads one whose members are all safe to stop); drop its queue tickets; one audit
    line to the project's checks waits log. Returns the record logged, with "text". When this outlasts its
    wait plus PROBE_S, a record with "timeout" and "text" (its thread still finishes the stop; a later pass
    sees the process again if it lives on); None while an earlier stop or probe is still stuck on it."""
    from . import checkcap, leftoverstop
    guarded = leftoverstop._guarded(p) if guarded is None else guarded
    pid = found["pid"]
    term_s = leftoverstop.TERM_S if term_s is None else term_s

    def group_ok(g: int) -> bool:
        group = leftoverstop._members(g)
        return bool(group) and not (guarded & set(group)) and not any(
            procowner.foreign_env(p.base, leftoverstop._read_env(m)) or leftoverstop._uid(m) != os.getuid()
            or leftoverstop._untouchable(p, m, leftoverstop._read_env(m) or {}) for m in group)

    def stop_it() -> dict:
        lead = leftoverstop._pgrp(pid) == pid and pid != os.getpgrp()
        whole = lead and group_ok(pid)
        target = {"pid": pid, "start": found["start"], **({"pgid": pid} if lead else {})}
        signalled = leftoverstop.end([target], term_s, group_ok if lead else None)
        for t in found["tickets"]:
            locks.dequeue(Path(t))   # the ticket and its owner record: the waiter is gone
        rec = {"reaped": pid, "run_dir": found["run_dir"], "reason": found["why"], "ts": round(time.time(), 3),
               "signalled": bool(signalled), "group": bool(whole and signalled),
               "tickets": [os.path.basename(t) for t in found["tickets"]]}
        checkcap._append(Path(p.state), checkcap.WAITS_LOG, checkcap.WAITS_OLD, checkcap.WAITS_LOCK,
                         checkcap.WAITS_MAX_BYTES, rec)
        return rec

    wait = term_s + 2.0 + PROBE_S   # end waits term_s, then up to 2 s after SIGKILL
    state, rec = _bounded(pid, stop_it, wait)
    if state == "stuck":
        return None
    if state == "timeout":   # its thread goes on: it still drops the tickets and writes the audit line
        return {"pid": pid, "timeout": True,
                "text": f"checks reap: stopping leftover ttp checks pid {pid} ({found['why']}) took over "
                        f"{wait:.0f} s: left to finish in the background, or for a later pass"}
    rec["text"] = (f"checks reap: stopped leftover ttp checks pid {pid} ({found['why']}; run dir "
                   f"{found['run_dir'] or 'none'}): " + ("SIGTERM, then SIGKILL if still alive" if rec["signalled"]
                                                         else "already ended")
                   + (f"; dropped {len(found['tickets'])} queue ticket(s)" if found["tickets"] else ""))
    return rec


def reap_one(p, pid: int, slots: Path | None = None, guarded: set[int] | None = None,
             dry_run: bool = False) -> list[dict]:
    """check `pid`, then stop it when it is a leftover (unless `dry_run`). Returns the records to log, each with
    "text": a stop's (stop), a dry run's found entry, or one when its reads or stop time out (once: none while
    an earlier one is still stuck on it)."""
    from . import checkcap, leftoverstop
    slots = Path(checkcap.slot_dir() if slots is None else slots)
    guarded = leftoverstop._guarded(p) if guarded is None else guarded
    verdict, f = check(p, pid, slots, guarded)
    if verdict == "timeout":
        return [{"pid": pid, "timeout": True, "text": f"checks reap: reading process {pid} took over {PROBE_S:.0f} s "
                                                      "(stuck in the kernel?): left for a later pass"}]
    if verdict != "leftover":
        return []
    if dry_run:
        return [{**f, "text": f"checks reap (dry run): would stop ttp checks pid {f['pid']} ({f['why']}; "
                              f"run dir {f['run_dir'] or 'none'})"}]
    rec = stop(p, f, guarded)
    return [rec] if rec else []


def reap(p, dry_run: bool = False) -> list[dict]:
    """reap_one for every process (unless `dry_run`, read only). Returns the records (dry run: the found
    entries with "text")."""
    from . import checkcap, leftoverstop
    slots, guarded = Path(checkcap.slot_dir()), leftoverstop._guarded(p)
    return [r for pid in pids() for r in reap_one(p, pid, slots, guarded, dry_run)]
