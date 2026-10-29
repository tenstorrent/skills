# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Slots of a shared resource (a device, a remote build directory) as OS file locks.

Every holder takes one slot file under state/locks: `ttp lock` for one command, the run supervisor
of an `exclusive:<name>` task for its whole run. A lock ends with the process that holds it, so a
crash or a reboot never leaves a resource taken.

Waiters queue in arrival order (a ticket file per waiting process under <resource>.queue): only the
first `slots` live tickets may take a slot, so a waiter is never starved by later arrivals. A ticket
whose process is gone is dropped.

A shared holder (`ttp lock --shared`, for read-only work) takes a read lock on every slot instead
of one slot's write lock, and labels itself in <resource>.shared: shared holders run together, and
any slot's exclusive holder keeps them all out, as they keep it out. The queue stays in arrival
order across modes: a shared waiter goes only once no exclusive waiter is ahead of it, and an
exclusive one only once no shared waiter is.

Resources named in config `device.locks` are one device: they all map to the first name's lock, so
at most one command at a time is in the device phase, whatever name it locks by.

An exclusive task that finds every slot held reserves the resource: new `ttp lock` commands wait
until it has its slot, so commands that keep taking the lock in turn cannot starve it. Whoever
reserves refreshes the reservation while it waits; one not refreshed for RESERVE_STALE_S no longer
counts, so a crashed daemon or supervisor never wedges the resource.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import timefmt
from .project import durable_write, zombie

RESERVE_STALE_S = 120


def _device_list(cfg: dict) -> list[str]:
    return [str(x) for x in ((cfg.get("device") or {}).get("locks") or [])]


def device_locks(cfg: dict) -> set[str]:
    return set(_device_list(cfg))


def canonical(cfg: dict, resource: str) -> str:
    """The lock a resource name is held by: every device name shares the first one's lock."""
    dev = _device_list(cfg)
    return dev[0] if resource in dev else resource


def slot_paths(locks_dir: Path, resource: str, slots: int) -> list[Path]:
    return [Path(locks_dir) / f"{resource}.{i}.lock" for i in range(max(int(slots or 1), 1))]


def try_take(paths: list[Path], holder: str, what: str = "", key: str = "", extra: dict | None = None):
    """The open file of the first free slot, now held and labelled with its holder (and `key`, what
    the holder runs, when given, and the fields of `extra`, such as job_fields); None if all are taken."""
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            continue
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"holder": holder, "since": time.time(), "command": what[:300], **({"key": key} if key else {}),
                            **(extra or {}), **taker()}))
        f.flush()
        return f
    return None


def relabel(f, fields: dict) -> None:
    """Add `fields` to the label of the held slot file `f` (try_take's), keeping the rest."""
    try:
        f.seek(0)
        h = json.loads(f.read() or "{}")
        f.seek(0)
        f.truncate()
        f.write(json.dumps({**(h if isinstance(h, dict) else {}), **fields}))
        f.flush()
    except (OSError, ValueError, TypeError):
        pass


def _pid_ns() -> str | None:
    """This process's PID namespace ("" on hosts without them); None when it cannot tell."""
    if not sys.platform.startswith("linux"):
        return ""
    try:
        if int(os.readlink("/proc/self")) != os.getpid():
            return None
        return os.readlink("/proc/self/ns/pid")
    except (OSError, ValueError):
        return None


def _owner(pid: int) -> dict:
    """The fields a slot label gives the process that holds it: its pid, and where that pid means
    something (host and PID namespace), so a reader elsewhere does not judge it."""
    return {"pid": int(pid), "node": os.uname().nodename, "pidns": _pid_ns()}


PF_KTHREAD = 0x00200000   # /proc/<pid>/stat flags: a kernel thread


def kthread(pid, proc: str = "/proc") -> bool:
    """Whether `pid` is a kernel thread in this process's /proc (PF_KTHREAD in its stat flags): it lives
    for the whole boot, so it never names a lock's holder. False when it is gone or this cannot tell."""
    try:
        return bool(int(Path(f"{proc}/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[6]) & PF_KTHREAD)
    except (OSError, IndexError, ValueError, TypeError):
        return False


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def taker(env=None) -> dict:
    """The fields a take gives the process that owns its hold (a slot label, the activity log): pid,
    pgid, node and pidns. That is this process, except in a PID namespace other than its run's (TTP_PIDNS;
    a `ttp sandbox`), where its own pid (often 2 or 13: a kernel thread's on the host) means nothing to a
    reader there: it names the run's agent instead (child.pid in TTP_RUN_DIR, the leader of its own
    process group) and keeps its own as ns_pid and ns_pidns. A candidate that is a kernel thread is never
    named: the fields fall back to this process and say so in owner_fallback."""
    env = os.environ if env is None else env
    me = os.getpid()
    out = {**_owner(me), "pgid": _pgid(me)}
    run_ns = env.get("TTP_PIDNS")
    if not run_ns or run_ns == out["pidns"] or not env.get("TTP_RUN_DIR"):
        return out
    try:
        agent = int((Path(env["TTP_RUN_DIR"]) / "child.pid").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return out
    if agent <= 2 or kthread(agent):
        return {**out, "owner_fallback": f"the run's agent pid {agent} is a kernel thread"}
    return {**out, "pid": agent, "pgid": agent, "pidns": run_ns, "ns_pid": me, "ns_pidns": out["pidns"]}


def job_fields(env=None) -> dict:
    """What a slot label adds for a taker inside a `ttp detach` job (TTP_DETACHED and the job's .rc in
    TTP_DETACH_RC): the job's name, its .rc and the pid `ttp detach` recorded for it (its wrapper's, from
    its .started file) as job, job_rc and job_pid; {} anywhere else."""
    from .progress import JOB_ENV
    env = os.environ if env is None else env
    rc = env.get(JOB_ENV)
    if not rc or not env.get("TTP_DETACHED"):
        return {}
    from .detach import started
    try:
        pid = int(started(Path(rc)).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pid = 0
    return {"job": Path(rc).stem, "job_rc": str(rc), **({"job_pid": pid} if pid > 1 else {})}


def _job_note(h: dict) -> str:
    """" (detached job <name>, job pid N)" for a label job_fields filled, else ""."""
    if not h.get("job"):
        return ""
    return f" (detached job {h['job']}" + (f", job pid {h['job_pid']}" if h.get("job_pid") else "") + ")"


def label_pid(f, pid: int) -> None:
    """Names `pid` as the holder process of the held slot file `f`: a keeper it was handed to. "pid" stays
    the live holder's, so whoever judges it sees the keeper, not the taker that exited; the label adds
    via "keeper" and the taker's pid as taker_pid."""
    try:
        f.seek(0)
        h = json.loads(f.read() or "{}")
        taker = h.get("ns_pid") or h.get("pid")
        h = {k: v for k, v in h.items() if k not in ("ns_pid", "ns_pidns", "owner_fallback")}   # the taker's
        f.seek(0)
        f.truncate()
        f.write(json.dumps({**h, **_owner(pid), "pgid": _pgid(pid), "via": "keeper",
                            **({"taker_pid": taker} if taker and taker != pid else {})}))
        f.flush()
    except (OSError, ValueError, TypeError):
        pass


def _pid_note(h: dict) -> str:
    """", pid N alive" (or "gone") for a label naming its holder process; only the pid when this
    process cannot judge it (another host or PID namespace, or a label from before pids)."""
    try:
        pid = int(h.get("pid") or 0)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    if _judgeable(h) and kthread(pid):
        return f", pid {pid} {UNVERIFIABLE}"
    return f", pid {pid}" + ((" alive" if _alive(pid) else " gone") if _judgeable(h) else "")


def pid_alive(h: dict) -> bool | None:
    """Whether the process a label or record names (its "pid": a keeper's once handed to one) lives; None
    when it names none or this process cannot judge it (_judgeable)."""
    try:
        pid = int(h.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return _alive(pid) if pid > 0 and _judgeable(h) and not kthread(pid) else None


UNVERIFIABLE = "is a kernel thread here: holder unverifiable"   # a label from a taker in another PID namespace


def _judgeable(h: dict) -> bool:
    """Whether this process can judge the pid a label or record names: it was seen on this host and in
    this PID namespace (another's pid may be any process here)."""
    return h.get("node") == os.uname().nodename and h.get("pidns") is not None and h.get("pidns") == _pid_ns()


def _worker_note(run: dict, run_id) -> tuple[str, bool]:
    """A run's worker pid and whether it lives, as _pid_note judges a label's: only when the run was
    recorded on this boot, host and PID namespace (its note's "pid_where", or this host for a record from
    before it), else "unknown", never alive: a pid from an earlier boot or another namespace may be
    reused by any process."""
    from .runner import same_boot
    wp = int(run.get("pid") or 0)
    try:
        where = json.loads(run.get("note") or "{}").get("pid_where")
    except (ValueError, TypeError, AttributeError):
        where = None
    if not same_boot(run.get("boot_id")):
        return f", pid {wp} unknown (run {run_id}'s worker, recorded on an earlier boot)", False
    if isinstance(where, dict) and not _judgeable(where):
        return f", pid {wp} unknown (run {run_id}'s worker, seen on another host or PID namespace)", False
    live = _alive(wp)
    return f", pid {wp} {'alive' if live else 'gone'} (run {run_id}'s worker)", live


def _holder_note(h: dict, where=None) -> str:
    """What follows a holder's label and since when in a busy or wait line: its pid and whether it lives
    (_pid_note), never nothing. Where `where` is the holder's project, a task's hold is looked up in its
    run record: one with no pid in its label (from before pids) names its run's worker pid instead, else
    "pid unknown" and whether the run is still active or ended; the keeper of a task's detached jobs also
    names each job's own pid and whether it still runs. A hold whose task is done and that has no live
    pid (its own, its run's worker or a job's) is marked stale: it is not a live holder, though the lock
    stays taken until whatever process has its file open lets go."""
    from . import detach, hold
    from .project import Project
    note, lbl = _pid_note(h), hold.parse(h.get("holder"))
    if not isinstance(where, Project) or not lbl or lbl["project"] not in (None, where.name):
        return (note or ", pid unknown") + _job_note(h)
    try:   # an unreadable database or a run id that is not a number: only what the label says
        run = where.db.one("SELECT pid, status, dir, boot_id, note FROM runs WHERE id=?", (int(lbl["run"]),))
        task = where.db.one("SELECT status FROM tasks WHERE id=?", (lbl["task"],))
    except Exception:
        run = task = None
    live = note.endswith(" alive")
    if UNVERIFIABLE in note and run and int(run.get("pid") or 0) > 0:   # its run's worker says instead
        worker, live = _worker_note(run, lbl["run"])
        note += ";" + worker.removeprefix(",")
    if not note:
        wp = int(run.get("pid") or 0) if run else 0
        if wp > 0:
            note, live = _worker_note(run, lbl["run"])
        else:
            note = ", pid unknown" + (f" (run {lbl['run']} {'active' if run['status'] == 'running' else 'ended'})"
                                      if run else f" (no record of run {lbl['run']})")
    if lbl["kind"] == hold.LABEL and run and run["dir"]:
        jobs = []
        for rc in (hold.read(Path(run["dir"])) or {}).get("jobs") or []:
            rc = Path(rc)
            pid, runs = detach._entry(rc).get("pid"), not job_ended(rc)
            live = live or runs
            jobs.append(f"job {rc.stem} pid {pid if isinstance(pid, int) and pid > 1 else 'unknown'} "
                        f"{'running' if runs else 'ended'}")
        if jobs:
            note += "; " + ", ".join(jobs[:3]) + (f" +{len(jobs) - 3} more" if len(jobs) > 3 else "")
    note += _job_note(h)
    if task and task["status"] == "done" and not live:
        note += f"; stale: task #{lbl['task']} is done"
    return note


SHARED_MARK = " (shared)"   # ends a shared holder's label


def shared_dir(paths: list[Path]) -> Path:
    """Where the shared holders of the resource whose slots are `paths` label themselves."""
    return paths[0].parent / f"{paths[0].name.rsplit('.', 2)[0]}.shared"


class SharedHold:
    """A shared hold: read locks on every slot and the holder's label; close() ends it."""

    def __init__(self, files: list, label: Path):
        self.files, self.label = files, label

    def close(self) -> None:
        try:
            self.label.unlink()
        except OSError:
            pass
        for f in self.files:
            f.close()


def try_take_shared(paths: list[Path], holder: str, what: str = "", extra: dict | None = None) -> SharedHold | None:
    """A shared hold of the resource: a read lock on every slot, so no exclusive holder has one;
    None if one has. The label ends in SHARED_MARK."""
    files = []
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            f.close()
            for g in files:
                g.close()
            return None
        files.append(f)
    d = shared_dir(paths)
    try:
        d.mkdir(parents=True, exist_ok=True)
        name = f"{time.time():017.6f}-{os.getpid()}"
        # Locked under a hidden name first, then renamed in: a reader never sees it unlocked.
        tmp = d / f".{name}.tmp"
        lf = open(tmp, "a+")
        fcntl.flock(lf, fcntl.LOCK_EX)
        lf.write(json.dumps({"holder": holder + SHARED_MARK, "since": time.time(), "command": what[:300],
                             **(extra or {}), **taker()}))
        lf.flush()
        os.rename(tmp, d / name)
    except OSError:
        for g in files:
            g.close()
        raise
    return SharedHold(files + [lf], d / name)


def shared_holders(paths: list[Path]) -> list[dict]:
    """The labels ({holder, since, command}) of the resource's live shared holders, oldest first;
    labels whose process is gone are removed."""
    d, out = shared_dir(paths), []
    try:
        names = sorted(n for n in os.listdir(d) if not n.startswith("."))
    except OSError:
        return out
    for name in names:
        path = d / name
        if not _ticket_live(path):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        try:
            out.append(json.loads(path.read_text() or "{}"))
        except (OSError, ValueError):
            continue
    return out


def any_free(paths: list[Path]) -> bool:
    """Whether some slot is free right now. Testing takes the slot for an instant; a `ttp lock`
    that tries in that instant just retries."""
    for path in paths:
        if not path.exists():
            return True
        with open(path, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            return True
    return False


def holders(paths: list[Path], where=None, pids: bool = False) -> list[str]:
    """Each slot's holder label and since when, in the zone of `where` (the project; None is UTC),
    with its holder's pid and whether it lives when `pids`. A free slot keeps its last label: for who
    holds a slot now, use holders_now."""
    out = []
    for path in paths:
        try:
            h = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(h, dict) and h:
            out.append(f"{h.get('holder')} since {timefmt.short(float(h.get('since') or 0), where)}"
                       + (_holder_note(h, where) if pids else ""))
    return out


def held_labels(paths: list[Path]) -> list[str]:
    """The holder labels of the slots among paths whose lock is taken right now, then the
    resource's shared holders."""
    return _slot_labels(paths) + [str(h.get("holder") or "") for h in shared_holders(paths)] if paths else []


def holders_now(paths: list[Path], where=None) -> list[str]:
    """`holders` with pids (_holder_note), of only the slots whose lock is taken right now, then the shared
    holders: what a wait or busy message names."""
    out = holders([x for x in paths if _slot_labels([x])], where, pids=True)
    return out + [f"{h.get('holder')} since {timefmt.short(float(h.get('since') or 0), where)}{_holder_note(h, where)}"
                  for h in (shared_holders(paths) if paths else [])]


def holder_records(paths: list[Path], where=None) -> list[dict]:
    """holders_now as data (`ttp lock --probe --json`): each slot taken right now, then each shared holder
    (shared: true), as its label's fields (holder, since, command, pid, node, pidns; a slot handed to a
    keeper names the keeper in pid, with via "keeper" and taker_pid), plus alive (pid_alive), note
    (_holder_note's text) and stale (a done task's hold with no live pid)."""
    out = []
    for path in paths:
        if not _slot_labels([path]):
            continue
        try:
            h = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(h, dict) and h:
            out.append((h, False))
    out += [(h, True) for h in (shared_holders(paths) if paths else []) if isinstance(h, dict)]
    recs = []
    for h, sh in out:
        note = _holder_note(h, where)
        recs.append({**h, "shared": sh, "alive": pid_alive(h), "note": note.removeprefix(", "),
                     "stale": "; stale: " in note})
    return recs


_REDIRECT = re.compile(r"^\d*(?:>>?|<|>&|&>)")


def lock_probe_parts(cmd: str) -> tuple[list[tuple[str, bool]], list[str]]:
    """A wait probe's `ttp lock --probe [--shared] <resource>...` commands (joined by &&, || or ;, output
    redirections allowed) as (resource, shared), and its other commands as their text."""
    found, rest = [], []
    for part in re.split(r"&&|\|\||;", str(cmd or "")):
        try:
            w = shlex.split(part)
        except ValueError:
            return [], [str(cmd)]
        words, skip = [], False
        for x in w:
            if skip:
                skip = False
            elif _REDIRECT.match(x):
                skip = bool(_REDIRECT.fullmatch(x))   # `> file`: the file is the next word
            else:
                words.append(x)
        names = [x for x in words[2:] if not x.startswith("-")]
        if words[:2] == ["ttp", "lock"] and "--probe" in words and "--timeout" not in words and len(names) == 1:
            found += [(n, "--shared" in words) for n in names[0].split(",") if n]   # `--probe A,B`: each of them
        elif part.strip():
            rest.append(part.strip())
    return found, rest


def probe_resources(cmd: str) -> list[str]:
    """The resources a wait probe checks when it is only `ttp lock --probe <resource>` commands, else []."""
    found, rest = lock_probe_parts(cmd)
    return [] if rest else [r for r, _ in found]


def _slot_labels(paths: list[Path]) -> list[str]:
    """The holder labels of the slots among paths whose lock is taken right now (not shared)."""
    out = []
    for path in paths:
        try:
            with open(path) as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    continue
                except OSError:
                    pass
            out.append(str(json.loads(path.read_text() or "{}").get("holder") or ""))
        except (OSError, ValueError, AttributeError):
            continue
    return out


def held(locks_dir: Path, where=None) -> list[str]:
    """Who holds each resource right now, and who has one reserved: "device: task #3 (run 9) since
    10:02 UTC" (in the zone of `where`, the project). A slot file keeps its label after release, so only slots whose lock is taken count;
    testing takes a free one for an instant, which a `ttp lock` trying then just retries."""
    out, locks_dir = [], Path(locks_dir)
    try:
        slots = sorted(locks_dir.glob("*.lock"))
        marks = sorted(locks_dir.glob("*.reserved"))
    except OSError:
        return out
    for path in slots:
        try:
            with open(path) as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    continue
                except OSError:
                    pass
        except OSError:
            continue
        out += [f"{path.name.rsplit('.', 2)[0]}: {h}" for h in holders([path], where)]
    try:
        groups = sorted(locks_dir.glob("*.shared"))
    except OSError:
        groups = []
    for d in groups:
        res = d.name[:-len(".shared")]
        out += [f"{res}: {h.get('holder')} since {timefmt.short(float(h.get('since') or 0), where)}"
                for h in shared_holders([d.parent / f"{res}.0.lock"])]
    for path in marks:
        who = reserved_by(path)
        if who:
            out.append(f"{path.name[:-len('.reserved')]}: reserved for {who}")
    return out


def reserve_path(locks_dir: Path, resource: str) -> Path:
    return Path(locks_dir) / f"{resource}.reserved"


def reserve(path: Path, holder: str) -> None:
    """Reserve for holder, or refresh its reservation. A fresh one by another holder stays."""
    other = reserved_by(path)
    if other and other != holder:
        return
    try:
        since = json.loads(path.read_text()).get("since") if other else None
    except (OSError, ValueError):
        since = None
    path.parent.mkdir(parents=True, exist_ok=True)
    durable_write(path, json.dumps({"holder": holder, "since": since or time.time(), "ts": time.time()}))


def reserved_by(path: Path) -> str | None:
    """The holder of a live reservation; None when there is none or it went stale."""
    try:
        r = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return None
    age = time.time() - float(r.get("ts") or 0)
    # A stamp from the future means the clock went back; trusting it would hold the resource
    # until the clock caught up.
    if age > RESERVE_STALE_S or age < -5:
        return None
    return str(r.get("holder") or "") or None


def unreserve(path: Path, holder: str) -> None:
    try:
        if json.loads(path.read_text() or "{}").get("holder") == holder:
            path.unlink()
    except (OSError, ValueError):
        pass


WAITS_FILE = "lock_waits.json"


def record_wait(run_dir: Path, key: str, start: float, end: float | None) -> None:
    """Record one `ttp lock` wait of a run: open (end None) while it waits, closed once it ends.
    Several commands of one run may wait at once; the flock keeps their updates from overwriting
    each other, and the rename keeps readers from seeing half a file."""
    run_dir = Path(run_dir)
    try:
        with open(run_dir / f"{WAITS_FILE}.lock", "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            try:
                waits = json.loads((run_dir / WAITS_FILE).read_text())
            except (OSError, ValueError):
                waits = {}
            waits[key] = {"start": start, "end": end, "pid": os.getpid()}
            durable_write(run_dir / WAITS_FILE, json.dumps(waits))
    except OSError:
        pass    # a missing record only costs the run its extension, never the command


def waited(run_dir: Path, now: float | None = None) -> float:
    """Seconds the run has spent waiting in `ttp lock`, overlapping waits counted once. A wait still
    going counts up to now; one whose process died without closing it counts nothing."""
    now = time.time() if now is None else now
    try:
        waits = json.loads((Path(run_dir) / WAITS_FILE).read_text())
    except (OSError, ValueError):
        return 0.0
    spans = []
    for w in waits.values():
        try:
            start, end = float(w["start"]), w.get("end")
            if end is None:
                end = now if _alive(int(w.get("pid") or 0)) else start
            spans.append((start, min(float(end), now)))
        except (KeyError, TypeError, ValueError):
            continue
    total, reach = 0.0, float("-inf")
    for start, end in sorted(spans):
        if end > reach:
            total += end - max(start, reach)
            reach = end
    return total


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not zombie(pid)


def queue_dir(locks_dir: Path, resource: str) -> Path:
    return Path(locks_dir) / f"{resource}.queue"


_TICKETS: dict[Path, object] = {}   # this process's tickets: the open file that holds each one's lock


SHARED_TICKET = "-shared"   # ends a shared waiter's ticket name
GROUP_TICKET = ".g"         # then a fair queue's group id: <arrival>-<pid>[.p<priority>].g<id>
MULTI_TICKET = ".m"         # after the pid, a waiter for several resources at once (`ttp lock A,B`)
PRIORITY_TICKET = ".p"      # before it, the waiter's task priority (lowest first; checkcap uses 0 for obligation owners)
SERVED_KEEP_S = 86400.0     # a group's last-served time older than this no longer changes its turn


def group_id(group: str | None) -> str:
    """The short id a fair queue's ticket carries for its group (a project); "" for none."""
    return hashlib.sha256(group.encode()).hexdigest()[:12] if group else ""


def enqueue(locks_dir: Path, resource: str, holder: str, since: float | None = None,
            shared: bool = False, group: str | None = None, priority: int | None = None,
            multi: bool = False) -> Path:
    """A ticket for this process in the resource's arrival queue. Like a slot, it is an OS file lock
    held by this process, so it ends with it (a crash, a kill, a reboot) whatever its pid becomes.
    Not written durably: a power cut ends every waiter with it. Remove it with `dequeue`. `since`
    is the arrival time to queue at (default now): a waiter that hands over keeps its place.
    A shared waiter's ticket name ends in SHARED_TICKET. `group` (a project) is what a fair queue
    (fair_order) takes turns between; waiters without one form their own group. `priority` (the task's;
    lower first) orders the waiters within a group; it goes before the group id, so older runtimes
    still read the group. `multi`: one of the tickets of a waiter for several resources at once
    (MULTI_TICKET after the pid), which a waiter that holds locks already does not wait behind (my_turn)."""
    d = queue_dir(locks_dir, resource)
    d.mkdir(parents=True, exist_ok=True)
    gid = group_id(group)
    prio = f"{PRIORITY_TICKET}{int(priority)}" if priority is not None else ""
    name = (f"{time.time() if since is None else since:017.6f}-{os.getpid()}{MULTI_TICKET if multi else ''}{prio}"
            f"{GROUP_TICKET + gid if gid else ''}{SHARED_TICKET if shared else ''}")
    # Locked under a hidden name first, then renamed in: `queued` never sees it unlocked.
    tmp = d / f".{name}.tmp"
    f = open(tmp, "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    f.write(holder)
    f.flush()
    path = d / name
    os.rename(tmp, path)
    _TICKETS[path] = f
    return path


def dequeue(ticket: Path | None) -> None:
    if ticket is None:
        return
    try:
        ticket.unlink()
    except OSError:
        pass
    f = _TICKETS.pop(ticket, None)
    if f is not None:
        f.close()


def _ticket_live(path: Path) -> bool:
    try:
        with open(path) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            return False
    except OSError:
        return False


def queued(locks_dir: Path, resource: str) -> list[Path]:
    """Live tickets in arrival order; tickets whose process is gone are removed."""
    d = queue_dir(locks_dir, resource)
    out = []
    try:
        names = sorted(n for n in os.listdir(d) if not n.startswith("."))
    except OSError:
        return out
    for name in names:
        path = d / name
        if path in _TICKETS or _ticket_live(path):
            out.append(path)
        else:
            try:
                path.unlink()
            except OSError:
                pass
    return out


def is_multi(ticket: Path) -> bool:
    return MULTI_TICKET in ticket.name.partition(PRIORITY_TICKET)[0].partition(GROUP_TICKET)[0]


def is_shared(ticket: Path) -> bool:
    return ticket.name.endswith(SHARED_TICKET)


def ticket_group(ticket: Path) -> str:
    """A ticket's group id ("" for none)."""
    name = ticket.name.removesuffix(SHARED_TICKET)
    return name.rpartition(GROUP_TICKET)[2] if GROUP_TICKET in name else ""


def ticket_priority(ticket: Path) -> float:
    """A ticket's task priority; one without (older runtimes, no task) sorts after every priority."""
    name = ticket.name.removesuffix(SHARED_TICKET).partition(GROUP_TICKET)[0]
    try:
        return float(int(name.rpartition(PRIORITY_TICKET)[2])) if PRIORITY_TICKET in name else math.inf
    except ValueError:
        return math.inf


def _arrival(ticket: Path) -> float:
    try:
        return float(ticket.name.split("-", 1)[0])
    except ValueError:
        return 0.0


def served_dir(locks_dir: Path, resource: str) -> Path:
    return Path(locks_dir) / f"{resource}.served"


def mark_served(locks_dir: Path, resource: str, group: str | None) -> None:
    """Record that `group` just got the resource, so a fair queue serves the other groups first.
    Only a time: a stale or lost record just makes that group look longer unserved."""
    d = served_dir(locks_dir, resource)
    now = time.time()
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = d / (group_id(group) or "none")
        f.touch()
        os.utime(f, (now, now))
        for old in d.iterdir():
            if now - old.stat().st_mtime > SERVED_KEEP_S:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def _served(locks_dir: Path, resource: str) -> dict[str, float]:
    out = {}
    try:
        for f in served_dir(locks_dir, resource).iterdir():
            out["" if f.name == "none" else f.name] = f.stat().st_mtime
    except OSError:
        pass
    return out


def fair_order(locks_dir: Path, resource: str, q: list[Path] | None = None, urgent_max: int = 0) -> list[Path]:
    """The waiters in the order a fair queue serves them: each next turn goes to the oldest waiter
    of the group that has waited longest since it last got the resource (since its oldest waiter
    arrived, if later), so one group's burst cannot starve the others. Within a group by task priority
    (ticket_priority), then arrival.

    `urgent_max` > 0: urgent waiters (task priority URGENT_PRIORITY or better) go first, taking turns
    by group among themselves, but at most `urgent_max` turns in a row (counting the ones already
    served, mark_turn) while other waiters wait; then the next other waiter goes. Tickets without a
    priority (older runtimes) are never urgent, so a queue without urgent waiters keeps this order.

    Known gap: while some projects on a host still run a runtime from before fair ordering
    (arrival-order my_turn) or before urgent turns next to newer waiters, the orders can disagree on
    who goes first, so one slot may sit idle until another frees; this ends once every project on the
    host is upgraded."""
    q = queued(locks_dir, resource) if q is None else q
    groups: dict[str, list[Path]] = {}
    for t in q:
        groups.setdefault(ticket_group(t), []).append(t)
    served = _served(locks_dir, resource)
    key = {g: (0, max(served.get(g, 0.0), _arrival(ts[0])), ts[0].name) for g, ts in groups.items()}
    for ts in groups.values():
        ts.sort(key=ticket_priority)   # stable: arrival order within a priority
    hot = sum(map(_urgent, q)) if urgent_max > 0 else 0
    cold = len(q) - hot
    run = _streak(locks_dir, resource) if hot and cold else 0
    out = []
    while groups:
        want = run < urgent_max if hot and cold else None   # urgent or not; None: either
        g = min((g for g, ts in groups.items() if want is None or any(_urgent(t) == want for t in ts)),
                key=key.__getitem__)
        ts = groups[g]
        t = ts.pop(0 if want is None else next(i for i, t in enumerate(ts) if _urgent(t) == want))
        out.append(t)
        if hot and _urgent(t):
            hot, run = hot - 1, run + 1
        else:
            cold, run = cold - 1, 0
        if ts:
            key[g] = (1, len(out), "")
        else:
            del groups[g]
    return out


URGENT_PRIORITY = 1   # a waiter at this task priority or better is urgent in fair_order with urgent_max


def _urgent(ticket: Path) -> bool:
    return ticket_priority(ticket) <= URGENT_PRIORITY


def streak_path(locks_dir: Path, resource: str) -> Path:
    return Path(locks_dir) / f"{resource}.urgent"


def _streak(locks_dir: Path, resource: str) -> int:
    try:
        return max(int(streak_path(locks_dir, resource).read_text().strip() or 0), 0)
    except (OSError, ValueError):
        return 0


def mark_turn(locks_dir: Path, resource: str, ticket: Path | None) -> None:
    """Record a turn for fair_order's `urgent_max`: one more urgent turn in a row when `ticket` is
    urgent and another waiter that is not still waits, else none. Call it once the ticket took the
    resource, before dequeue; None for a take with nobody queued. Only a count: a lost or raced write
    shifts the bound by a turn, never stops the queue."""
    n = 0
    if ticket is not None and _urgent(ticket):
        if any(not _urgent(t) for t in queued(locks_dir, resource) if t != ticket):
            n = _streak(locks_dir, resource) + 1
    if n == _streak(locks_dir, resource):
        return
    path = streak_path(locks_dir, resource)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(str(n))
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)


def my_turn(locks_dir: Path, resource: str, ticket: Path, slots: int, fair: bool = False,
            urgent_max: int = 0, past_multi: bool = False) -> bool:
    """Whether this ticket may take the resource now. An exclusive one: among the first `slots`
    waiters (urgent tickets, task priority URGENT_PRIORITY or better, before the rest), one per free
    slot, with no shared waiter ahead. A shared one: no exclusive waiter
    ahead, so it never jumps one. `fair`: waiters are counted in fair_order (with `urgent_max`), not
    arrival order. `past_multi` (a waiter that holds locks already): waiters for several resources at
    once ahead are not counted, as one of them may wait for a lock this waiter's holder has: waiting
    behind it would never end."""
    q = queued(locks_dir, resource)
    if ticket not in q:
        return True
    if fair:
        q = fair_order(locks_dir, resource, q, urgent_max)
    else:   # urgent waiters (an urgent task's) first, arrival order within: none, and it is plain arrival
        q = sorted(q, key=lambda t: not _urgent(t))
    ahead = [t for t in q[:q.index(ticket)] if not (past_multi and is_multi(t))]
    if is_shared(ticket):
        return all(is_shared(t) for t in ahead)
    return not any(is_shared(t) for t in ahead) and len(ahead) < max(int(slots or 0), 0)


def take_in_turn(locks_dir: Path, resource: str, ticket: Path, paths: list[Path], holder: str, what: str = "",
                 fair: bool = False, past_multi: bool = False, extra: dict | None = None):
    """try_take (try_take_shared for a shared ticket), but only when this ticket's turn has come
    (my_turn), so a later waiter never takes the one slot an earlier one is about to."""
    if not turn(locks_dir, resource, ticket, paths, fair, past_multi):
        return None
    return (try_take_shared(paths, holder, what, extra=extra) if is_shared(ticket)
            else try_take(paths, holder, what, extra=extra))


def turn(locks_dir: Path, resource: str, ticket: Path, paths: list[Path], fair: bool = False,
         past_multi: bool = False) -> bool:
    """my_turn with the resource's free slots counted now: what take_in_turn checks before it takes."""
    if is_shared(ticket):
        return my_turn(locks_dir, resource, ticket, 0, past_multi=past_multi)
    return my_turn(locks_dir, resource, ticket, len(paths) - len(held_labels(paths)), fair, past_multi=past_multi)


def probe(locks_dir: Path, resource: str, paths: list[Path], shared: bool = False) -> bool:
    """Free now, nobody waiting and not reserved: a task that gave up waiting can come back. For a
    shared taker: no exclusive holder, no exclusive waiter and not reserved."""
    if reserved_by(reserve_path(locks_dir, resource)):
        return False
    if shared:
        return not _slot_labels(paths) and all(is_shared(t) for t in queued(locks_dir, resource))
    return any_free(paths) and not queued(locks_dir, resource)


def job_lock(rc: Path) -> Path:
    """The lock a `ttp detach` job holds while any of its processes lives, next to its .rc file."""
    return Path(rc).with_suffix(".lock")


def job_ended(rc: Path) -> bool:
    """Whether a detached job ended: it wrote its exit code, or it is gone without one (detach.state);
    one the daemon is yet to start has not."""
    from .detach import state
    return state(Path(rc))[0] in ("ended", "lost")


def job_probe(rcs: list, ttp: str = "ttp") -> str:
    """A `retry_when` that exits 0 once every one of these detached jobs ended with exit 0, 4 once all
    ended but one failed or is gone (detach.FAILED_RC), 1 before."""
    return f"{ttp} detach --check " + " ".join(shlex.quote(str(r)) for r in rcs)


def _parent(pid: int) -> int | None:
    """The parent of process pid; None if it is gone or this cannot tell."""
    try:   # Linux: field 4
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
        return int(out) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def in_run(run_dir: Path | None) -> bool:
    """Whether this process still runs under the run's agent (child.pid in the run's folder), so the
    run supervises it. A background (`nohup ... &`) driver is re-parented once its shell exits and then is not.
    True whenever it cannot tell: another PID namespace (a sandbox), no record, an unreadable parent."""
    try:
        agent = int((run_dir / "child.pid").read_text().split()[0])
    except (OSError, ValueError, IndexError, TypeError):
        return True
    if (ns := os.environ.get("TTP_PIDNS")):
        try:
            if os.readlink("/proc/self/ns/pid") != ns:
                return True
        except OSError:
            pass
    pid = os.getpid()
    for _ in range(256):
        if pid == agent:
            return True
        if pid <= 1:
            return False
        nxt = _parent(pid)
        if nxt is None:
            return True
        pid = nxt
    return True


def other_pid_ns() -> bool:
    """Whether this process may run in a PID namespace other than its run's agent's: inside a `ttp
    sandbox`, in another namespace than TTP_PIDNS, under a /proc of another namespace, or where it
    cannot tell. Hosts other than Linux have no PID namespaces."""
    from . import sandbox
    if sandbox.inside():
        return True
    if not sys.platform.startswith("linux"):
        return False
    try:
        if int(os.readlink("/proc/self")) != os.getpid():
            return True
        ns = os.readlink("/proc/self/ns/pid")
    except (OSError, ValueError):
        return True
    return bool((run_ns := os.environ.get("TTP_PIDNS")) and run_ns != ns)


def _ancestors() -> set[int]:
    """The pids above this process, up to and with init (this process too): an agent may be its PID
    namespace's init (a container's first process, or a test's parent right under `ttp sandbox`)."""
    out, pid = set(), os.getpid()
    while pid > 0 and pid not in out and len(out) < 256:
        out.add(pid)
        pid = _parent(pid) or 0
    return out


def under_run_agent(dbs: list[Path] | None = None) -> bool:
    """Whether a live run's agent is an ancestor of this process: the child.pid of a running run of
    this machine's projects (or of `dbs`, project databases), with the start token it recorded. A
    worker can unset TTP_RUN_ID and the like, but not change which process started it. A run whose
    agent's tools were re-parented (`nohup ... &` after its shell exited) is not seen. In a PID
    namespace of its own (`ttp sandbox`, or another than the run's TTP_PIDNS) the walk stops at the
    namespace's init and cannot see the agent: True there, and whenever the namespace cannot be read."""
    if other_pid_ns():
        return True
    import sqlite3
    from .globalcap import connect_ro, local_projects
    from .runner import proc_start
    above = _ancestors()
    for db in (dbs if dbs is not None else [d for _, d in local_projects()]):
        try:
            conn = connect_ro(db)
            try:
                rows = conn.execute("SELECT id, dir FROM runs WHERE status='running'").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        for rid, d in rows:
            try:
                pid_text, _, started = (Path(d or Path(db).parent / "runs" / str(rid)) / "child.pid").read_text().partition("\n")
                pid = int(pid_text)
            except (OSError, ValueError):
                continue
            if pid in above and (not started.strip() or proc_start(pid) == started.strip()):
                return True
    return False
