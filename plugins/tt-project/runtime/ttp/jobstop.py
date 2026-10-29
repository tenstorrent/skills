# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""End the `ttp detach` jobs of a task that ended or handed off, and `ttp detach --stop <rc>`.

A detached job outlives the run that started it, so a task cancelled, failed or out of attempts would
leave its jobs running: their process group, the device-broker jobs they queued and their `ttp lock`
slots. Each daemon tick (sweep) ends every still running job of such a task, unless an open task still
waits on it (its retry_when, on_pass_cmd, on_pass or next_step names the job's .rc or `<run>/<name>`).
It does the same, across all of the task's runs, for a task that handed off `done` (keeping a job its
own hand-off names) and for a `blocked` task whose own hand-off names none of its jobs (HANDED_OFF).
end() does it in this order:

1. SIGSTOP the job's whole process group, so a loop in it cannot resubmit what is ended next.
2. With the broker CLI (BROKER) on PATH: each `tt-device-mcp run` client process in the group names a
   broker job by its owner (the client's `--owner`, else its Unix user) and its full command; a live
   broker job (`status -j`) with that exact owner and command is ended once (`kill <id>`). Nothing
   else is: another owner's or another command's job stays. No CLI, or a status call that fails or
   does not parse, skips this step with a note.
3. detach.stop: <name>.lost records why (task and state), then SIGKILL to the group.
4. The job's own lock and every lock slot whose label names the job's .rc (locks.job_fields) are
   checked; a slot still held by a process of the stopped group is freed by ending that process.
5. One event names the task, job, group, broker jobs ended and locks freed.

The same pass stops such a task's detached `ttp checks` (checks.pid in any of its runs, end_checks):
SIGTERM to their group, so they end their check command and write checks.rc, then SIGKILL. And its
detached `ttp push` (a marker under the project's pushes/ naming the task, end_push), which leads a
session of its own outside every run: a queued one is recorded as failed before the daemon starts it,
a running one gets SIGTERM to its group (it ends its check and writes a failed outcome), then SIGKILL.
A push queue batch serves many tasks and is never stopped here.

`ttp detach --stop-pids <file>` stops the processes a task's own driver listed in a pid file (stop_pids),
for a driver started some other way than `ttp detach` (setsid and a pid file, its own launcher), so the
task need not rely on the driver's own stop path (a STOP file its loop may skip). See plan_pids.

A job counts as running while its job lock is held, whatever its .rc or .lost say: a daemon that dies
mid-stop finds it still running on its next tick and finishes the stop; a stopped one is skipped and
logs nothing new.

`ttp detach --stop <rc>` is the manual form: allowed for a job whose task has ended (cancelled, failed,
exhausted or done), or for an open priority-1 caller task (TTP_TASK) whose resources include one the
job's held slots hold. A sandboxed caller, which cannot signal the job, queues the request (QUEUE) and
the daemon carries it out on its next tick, checking it again.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import detach, locks
from .db import load_result
from .project import Project, durable_write, write_json

ENDED = ("cancelled", "failed", "exhausted")   # task states whose jobs the daemon stops
HANDED_OFF = ("done", "blocked")                # hand-offs whose jobs the daemon stops unless the hand-off names them
STOPPABLE = ENDED + ("done",)                   # task states whose jobs `ttp detach --stop` may stop
OPEN_WAITER_SKIP = ("done", "failed", "cancelled", "exhausted")
BROKER = "tt-device-mcp"
BROKER_TIMEOUT_S = 30
BROKER_LIVE = ("queued", "pending", "waiting", "running", "started")
SWEEP_EVERY_S = 30
SWEEP_DAYS = 14
FREE_WAIT_S = 2.0
CHECKS_TERM_S = 15.0   # detached checks end their check command on SIGTERM, which may take a few seconds
PUSH_TERM_S = 15.0     # likewise a detached push: it ends its check and writes its outcome
QUEUE = "jobstop-queue"
EVENT = "detached_job_stopped"


# Process groups ---------------------------------------------------------------------------------

def _stat(pid: int) -> tuple[int, int] | None:
    """(pgid, uid) of a live process (not a zombie), else None. Linux /proc only."""
    try:
        st = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if st[0] == "Z":
            return None
        uid = Path(f"/proc/{pid}").stat().st_uid
        return int(st[2]), uid
    except (OSError, IndexError, ValueError):
        return None


def members(pgid: int) -> list[int]:
    """The live processes of group pgid ([] where /proc cannot tell)."""
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if n.isdigit() and (s := _stat(int(n))) and s[0] == pgid:
            out.append(int(n))
    return out


def _argv(pid: int) -> list[str]:
    try:
        return [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1]]
    except OSError:
        return []


def group_of(rc: Path) -> int:
    """The process group of the job at this .rc: its wrapper's pid (it leads a session of its own), from
    the run's registry, else its <name>.started; 0 when no such group lives. A live wrapper must carry
    the .rc in its arguments, so a reused pid is never taken for it."""
    rc = Path(rc)
    pid = detach._entry(rc).get("pid")
    if not (isinstance(pid, int) and pid > 1):
        try:
            pid = int(detach.started(rc).read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return 0
    if _stat(pid) is not None and Path("/proc").is_dir():
        if str(rc) not in _argv(pid):
            return 0
        return pid
    try:
        os.killpg(pid, 0)   # the wrapper is gone; its group lives while any member does
    except ProcessLookupError:
        return 0
    except PermissionError:
        pass
    return pid


def running(rc: Path) -> bool:
    """Whether any process of the job still lives: its job lock is held."""
    return not locks.any_free([locks.job_lock(Path(rc))])


# Broker jobs ------------------------------------------------------------------------------------

def _client(argv: list[str]) -> tuple[str, str, str] | None:
    """(owner, full command, command after `--`) of a `tt-device-mcp run` client's argv, else None."""
    for i, a in enumerate(argv[:-1]):
        if os.path.basename(a) == BROKER and argv[i + 1] == "run":
            rest, owner = argv[i + 2:], ""
            j = 0
            while j < len(rest) and rest[j] != "--" and rest[j].startswith("-"):
                if rest[j] == "--owner" and j + 1 < len(rest):
                    owner = rest[j + 1]
                    j += 2
                    continue
                if rest[j].startswith("--owner="):
                    owner = rest[j].split("=", 1)[1]
                j += 1
            cmd = rest[j + 1:] if j < len(rest) and rest[j] == "--" else rest[j:]
            return owner, " ".join(rest), " ".join(cmd)
    return None


def _jobs_listed(out: str) -> list[dict] | None:
    try:
        data = json.loads(out)
    except ValueError:
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else None


def end_broker_jobs(pids: list[int]) -> tuple[list[str], str]:
    """End the live broker jobs whose owner and exact full command match a `tt-device-mcp run` client
    among pids, once each. Returns (job ids ended, note: why the step was skipped or a kill failed)."""
    clients = []
    for pid in pids:
        c = _client(_argv(pid))
        if c is None:
            continue
        owner = c[0]
        if not owner:
            try:
                owner = pwd.getpwuid(Path(f"/proc/{pid}").stat().st_uid).pw_name
            except (OSError, KeyError):
                continue
        clients.append((owner, {c[1], c[2]} - {""}))
    if not clients:
        return [], ""
    cli = shutil.which(BROKER)
    if not cli:
        return [], f"{BROKER} is not on PATH: its broker jobs were not ended"
    try:
        r = subprocess.run([cli, "status", "-j"], capture_output=True, text=True, timeout=BROKER_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], f"`{BROKER} status -j` failed ({e}): its broker jobs were not ended"
    jobs = _jobs_listed(r.stdout) if r.returncode == 0 else None
    if jobs is None:
        return [], (f"`{BROKER} status -j` failed (exit {r.returncode}) or did not list jobs: its broker jobs "
                    "were not ended")
    ended, notes = [], []
    for j in jobs:
        jid = str(j.get("job_id") if j.get("job_id") is not None else j.get("id") or "")
        if not jid or jid in ended or j.get("finished_at") or str(j.get("status") or "running") not in BROKER_LIVE:
            continue
        owner, cmd = str(j.get("owner") or ""), str(j.get("command") or "")
        if not any(owner == o and cmd in cmds for o, cmds in clients):
            continue
        try:
            k = subprocess.run([cli, "kill", jid], capture_output=True, text=True, timeout=BROKER_TIMEOUT_S,
                               stdin=subprocess.DEVNULL)
            if k.returncode == 0:
                ended.append(jid)
            else:
                notes.append(f"`{BROKER} kill {jid}` exited {k.returncode}")
        except (OSError, subprocess.TimeoutExpired) as e:
            notes.append(f"`{BROKER} kill {jid}` failed ({e})")
    return ended, "; ".join(notes)


# Lock slots -------------------------------------------------------------------------------------

def lock_dirs(p: Project) -> list[Path]:
    """The project's lock folder and those of the shared resources it names."""
    from . import shared
    out = [p.state / "locks"]
    try:
        out += [shared.root() / r for r in sorted(shared.names(p.config()))]
    except Exception:
        pass
    return out


def job_slots(p: Project, rc: Path) -> list[Path]:
    """The lock slots held right now whose label names this job's .rc (job_rc)."""
    out = []
    for d in lock_dirs(p):
        try:
            slots = sorted(d.glob("*.lock"))
        except OSError:
            continue
        for s in slots:
            try:
                h = json.loads(s.read_text() or "{}")
            except (OSError, ValueError):
                continue
            if isinstance(h, dict) and h.get("job_rc") == str(rc) and not locks.any_free([s]):
                out.append(s)
    return out


def _resource(slot: Path) -> str:
    return slot.name.rsplit(".", 2)[0]


def _free(slots: list[Path], pgid: int, pids: set[int]) -> tuple[list[str], list[str]]:
    """Wait a moment for each slot to go free; one still held by a process of the stopped group is freed
    by ending that process. Returns (freed, still held) as slot names."""
    deadline = time.monotonic() + FREE_WAIT_S
    left = list(slots)
    while left and time.monotonic() < deadline:
        left = [s for s in left if not locks.any_free([s])]
        if left:
            time.sleep(0.05)
    for s in left:
        try:
            pid = int(json.loads(s.read_text() or "{}").get("pid") or 0)
        except (OSError, ValueError, TypeError):
            pid = 0
        st = _stat(pid) if pid > 1 else None
        if st is not None and (pid in pids or st[0] == pgid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    deadline = time.monotonic() + FREE_WAIT_S
    while left and time.monotonic() < deadline:
        left = [s for s in left if not locks.any_free([s])]
        if left:
            time.sleep(0.05)
    return [s.name for s in slots if s not in left], [s.name for s in left]


# The stop ---------------------------------------------------------------------------------------

def end(p: Project, rc: Path, why: str, task: int | None = None) -> dict | None:
    """Stop the running job at this .rc (steps 1-5 above). Its summary, also logged as an event, or None
    when it no longer runs (nothing done, nothing logged)."""
    rc = Path(rc)
    if not running(rc):
        return None
    pgid = group_of(rc)
    fp = f"job_stopped:{rc}:{pgid}"
    if not pgid and p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return None   # its holder left the group: said once, nothing more to do
    slots = [locks.job_lock(rc), *job_slots(p, rc)]
    pids: list[int] = []
    if pgid:
        try:
            os.killpg(pgid, signal.SIGSTOP)
        except OSError:
            pass
        pids = members(pgid)
    ids, note = end_broker_jobs(pids)
    if pgid:
        detach.stop(rc, pgid, why)
    freed, held = _free(slots, pgid, set(pids))
    out = {"task": task, "job": rc.stem, "rc": str(rc), "pgid": pgid, "broker_jobs": ids, "freed": freed,
           "held": held, "note": note if pgid else "no live process group found for it"}
    text = (f"stopped detached job {rc.stem}" + (f" (task #{task})" if task else "") + f": {why}; group {pgid or '?'}"
            + f"; broker jobs ended: {', '.join(ids) or 'none'}; locks freed: {', '.join(freed) or 'none'}"
            + (f"; still held: {', '.join(held)}" if held else "") + (f"; {out['note']}" if out["note"] else ""))
    out["text"] = text
    if not p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
               (time.time(), "daemon", EVENT, fp, "low", text, json.dumps(out), "handled", task))
    return out


def end_checks(p: Project, run_dir: Path, why: str, task: int | None = None) -> dict | None:
    """Stop the run's detached `ttp checks` (checks.pid) while they still run: SIGTERM to their group,
    which makes them end their check command and write checks.rc (143); SIGKILL after CHECKS_TERM_S,
    as a newer `ttp checks --detach` does. One event per checks process. None when none ran."""
    from .cli import CHECKS_RC, _checks_alive, _read_checks_pid
    info = _read_checks_pid(run_dir)
    if not _checks_alive(info):
        return None
    pid = info["pid"]
    for sig, wait in ((signal.SIGTERM, CHECKS_TERM_S), (signal.SIGKILL, FREE_WAIT_S)):
        try:
            os.killpg(pid, sig)
        except OSError:
            pass
        deadline = time.monotonic() + wait
        while _checks_alive(info) and time.monotonic() < deadline:
            time.sleep(0.1)
        if not _checks_alive(info):
            break
    rc = run_dir / CHECKS_RC
    text = (f"stopped the detached checks of run {run_dir.name} (task #{task}): {why}; group {pid}; "
            f"checks.rc {rc.read_text().strip() if rc.exists() else 'not written'}")
    fp = f"checks_stopped:{run_dir}:{pid}:{info.get('started')}"
    out = {"task": task, "job": "checks", "rc": str(rc), "pgid": pid, "text": text}
    if not p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
               (time.time(), "daemon", EVENT, fp, "low", text, json.dumps(out), "handled", task))
    return out


def end_push(p: Project, marker: Path, why: str, task: int | None = None) -> dict | None:
    """Stop the detached `ttp push` of this marker while it is queued or runs: a queued one is recorded
    as failed under its run lock, so the daemon never starts it; a running one (its run lock held) gets
    SIGTERM to its group, then SIGKILL after PUSH_TERM_S, and its marker then says why. A pid that no
    longer carries the marker in its arguments is never signalled. One event per push; None when it
    neither waits nor runs."""
    from . import push
    marker = Path(marker)
    m = push._read(marker)
    if m.get("kind") == "batch" or m.get("status") not in ("queued", "running"):
        return None
    run_lock = push._run_lock(marker)
    pid = m.get("pid")
    if m.get("status") == "queued":
        lock = locks.try_take([run_lock], f"detached push {m.get('id')}", "jobstop")
        if lock is None:
            return None   # the daemon is starting it right now: stopped on the next pass
        with lock:
            m = push._read(marker)
            if m.get("status") != "queued":
                return None
            m.update(status="failed", exit=None, ended=time.time(), reason=f"not started: {why}")
            write_json(marker, m)
        push._forget_lock(marker)
        pid = 0
    else:
        if locks.any_free([run_lock]):
            return None   # it ended: push._settle records the outcome
        if not (isinstance(pid, int) and pid > 1) or str(marker) not in _argv(pid):
            return None
        for sig, wait in ((signal.SIGTERM, PUSH_TERM_S), (signal.SIGKILL, FREE_WAIT_S)):
            try:
                os.killpg(pid, sig)
            except OSError:
                pass
            deadline = time.monotonic() + wait
            while not locks.any_free([run_lock]) and time.monotonic() < deadline:
                time.sleep(0.1)
            if locks.any_free([run_lock]):
                break
        m = push._settle(marker)
        if m.get("status") == "failed":
            m["reason"] = f"stopped: {why}" + (f" ({m['reason']})" if m.get("reason") else "")
            write_json(marker, m)
    text = (f"stopped the detached push {marker.stem}" + (f" (task #{task})" if task else "")
            + f": {why}; " + (f"group {pid}; marker {m.get('status')}" if pid else "it was queued and never started"))
    fp = f"push_stopped:{marker}:{pid}"
    out = {"task": task, "job": "push", "rc": str(marker), "pgid": pid, "text": text}
    if not p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
               (time.time(), "daemon", EVENT, fp, "low", text, json.dumps(out), "handled", task))
    return out


def ended_pushes(p: Project) -> list[tuple[Path, int, str]]:
    """(marker, task, state) of each queued or running detached push of a task in ENDED."""
    from . import push
    found = []
    for marker in sorted((p.state / push.DETACHED).glob("*.json")):
        m = push._read(marker)
        if m.get("kind") != "batch" and m.get("status") in ("queued", "running") and str(m.get("task") or "").isdigit():
            found.append((marker, int(m["task"])))
    out = []
    for marker, task in found:
        row = p.db.one("SELECT status FROM tasks WHERE id=?", (task,))
        if row and row["status"] in ENDED:
            out.append((marker, task, row["status"]))
    return out


def names_job(result: dict, rc: Path) -> bool:
    """Whether this hand-off's retry_when, on_pass_cmd, on_pass or next_step names the job: its .rc, or
    `<run>/<name>` as `ttp detach --check <dir>/<name>` writes it (a longer name such as <name>2 is not it).
    A file of another kind (a push marker's .json) counts only by its full name."""
    rc = Path(rc)
    text = " ".join(str(result.get(k) or "") for k in ("retry_when", "on_pass_cmd", "next_step"))
    text += " " + json.dumps(result.get("on_pass") or "")
    names = [re.escape(f"{rc.parent.name}/{rc.name}") + r"(?![\w-])"]
    if rc.suffix == ".rc":
        names.append(re.escape(f"{rc.parent.name}/{rc.stem}") + r"(?![\w.-])")
    return re.search(rf"(?<![\w.-])(?:{'|'.join(names)})", text) is not None


def waited_on(p: Project, rc: Path) -> int | None:
    """An open task whose hand-off names this job (names_job), else None."""
    rc = Path(rc)
    marks = ",".join("?" * len(OPEN_WAITER_SKIP))
    for t in p.db.q(f"SELECT id, result FROM tasks WHERE status NOT IN ({marks}) AND result LIKE ?",
                    (*OPEN_WAITER_SKIP, f"%{rc.parent.name}/{rc.stem}%")):
        if names_job(load_result(t["result"]), rc):
            return t["id"]
    return None


def _live_jobs(run_dir: Path) -> list[Path]:
    """The .rc of each job in this run's registry that is not closed and still runs."""
    try:
        jobs = json.loads((Path(run_dir) / detach.REGISTRY).read_text())
    except (OSError, ValueError):
        return []   # a run without detached jobs has no registry
    return [Path(j["rc"]) for j in (jobs if isinstance(jobs, list) else [])
            if isinstance(j, dict) and j.get("rc") and j.get("status") not in detach.CLOSED and running(Path(j["rc"]))]


def task_of(p: Project, rc: Path) -> dict | None:
    """The task whose run started the job at this .rc."""
    d = Path(rc).parent
    return p.db.one("SELECT t.* FROM runs r JOIN tasks t ON t.id=r.task WHERE r.dir IN (?,?) ORDER BY r.id DESC LIMIT 1",
                    (str(d), str(d.resolve())))


def sweep(p: Project, now: float | None = None) -> list[dict]:
    """The daemon's pass: stop each running job, detached checks and detached push of a task in ENDED that
    no open task waits on, then carry out queued `ttp detach --stop` requests. The summaries of the jobs
    it stopped."""
    now = time.time() if now is None else now
    out = []
    marks = ",".join("?" * len(ENDED))
    rows = p.db.q(f"SELECT r.dir, t.id, t.status FROM runs r JOIN tasks t ON t.id=r.task WHERE t.status IN ({marks}) "
                  "AND r.dir IS NOT NULL AND COALESCE(r.started, 0)>?", (*ENDED, now - SWEEP_DAYS * 86400))
    seen = set()
    for row in rows:
        reg = Path(row["dir"]) / detach.REGISTRY
        if row["dir"] in seen:
            continue
        seen.add(row["dir"])
        r = end_checks(p, Path(row["dir"]), f"task #{row['id']} is {row['status']}", row["id"])
        if r:
            out.append(r)
        try:
            jobs = json.loads(reg.read_text())
        except (OSError, ValueError):
            continue
        for j in jobs if isinstance(jobs, list) else []:   # a run without detached jobs has no registry
            if not (isinstance(j, dict) and j.get("rc")) or j.get("status") in detach.CLOSED:
                continue
            rc = Path(j["rc"])
            if not running(rc) or waited_on(p, rc) is not None:
                continue
            r = end(p, rc, f"task #{row['id']} is {row['status']}", row["id"])
            if r:
                out.append(r)
    out += _sweep_handed_off(p, now)
    for marker, task, state in ended_pushes(p):
        if waited_on(p, marker) is None and (r := end_push(p, marker, f"task #{task} is {state}", task)):
            out.append(r)
    out += serve_requests(p)
    return out


def _sweep_handed_off(p: Project, now: float) -> list[dict]:
    """Stop the live jobs, across all its runs, of a task that handed off done or blocked: all but those its
    own hand-off names (a blocked one keeps them all when it names any) or an open task waits on."""
    out = []
    marks = ",".join("?" * len(HANDED_OFF))
    rows = p.db.q(f"SELECT r.dir, t.id, t.status, t.result FROM runs r JOIN tasks t ON t.id=r.task "
                  f"WHERE t.status IN ({marks}) AND r.dir IS NOT NULL AND COALESCE(r.started, 0)>? ORDER BY t.id",
                  (*HANDED_OFF, now - SWEEP_DAYS * 86400))
    tasks: dict[int, tuple[str, dict, list[Path]]] = {}
    seen = set()
    for row in rows:
        if row["dir"] in seen:
            continue
        seen.add(row["dir"])
        if live := _live_jobs(Path(row["dir"])):
            tasks.setdefault(row["id"], (row["status"], load_result(row["result"]), []))[2].extend(live)
    for tid, (status, result, jobs) in tasks.items():
        kept = [rc for rc in jobs if names_job(result, rc)]
        if status == "blocked" and kept:
            continue
        for rc in jobs:
            if rc in kept or waited_on(p, rc) is not None:
                continue
            if r := end(p, rc, f"task #{tid} handed off {status}", tid):
                out.append(r)
    return out


# `ttp detach --stop` ----------------------------------------------------------------------------

def job_resources(p: Project, rc: Path) -> set[str]:
    cfg = p.config()
    return {locks.canonical(cfg, _resource(s)) for s in job_slots(p, rc)}


def may_stop(p: Project, rc: Path, caller: str | None) -> tuple[bool, str]:
    """(allowed, why) for `ttp detach --stop` of the job at rc by caller task id (TTP_TASK, may be empty)."""
    from .coordinator import task_resources
    t = task_of(p, rc)
    if t is None:
        return False, f"no task of this project started the job at {rc}"
    if t["status"] in STOPPABLE:
        return True, f"task #{t['id']} is {t['status']}"
    me = p.db.task(int(caller)) if caller and str(caller).isdigit() else None
    if me is None or me["status"] in OPEN_WAITER_SKIP or int(me["priority"]) != 1:
        return False, (f"refused: its task #{t['id']} is {t['status']}; only a job of an ended task, or one holding "
                       "a resource of the calling open priority-1 task, may be stopped")
    cfg = p.config()
    mine = {locks.canonical(cfg, r) for r in task_resources(me)}
    common = sorted(mine & job_resources(p, rc))
    if not common:
        return False, (f"refused: its task #{t['id']} is {t['status']} and the job holds none of task #{me['id']}'s "
                       "resources")
    return True, f"priority-1 task #{me['id']} needs {', '.join(common)}"


def request(p: Project, rc: Path, caller: str | None) -> Path:
    """Queue a stop for the daemon (a sandboxed caller cannot signal the job)."""
    q = p.state / QUEUE / f"{Path(rc).parent.name}-{Path(rc).stem}.json"
    q.parent.mkdir(parents=True, exist_ok=True)
    write_json(q, {"rc": str(rc), "caller": caller or "", "at": time.time()})
    return q


def serve_requests(p: Project) -> list[dict]:
    out = []
    folder = p.state / QUEUE
    for q in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            m = json.loads(q.read_text())
            if m.get("pidfile"):
                out += _serve_pids(p, m)
                q.unlink(missing_ok=True)
                continue
            rc = Path(m["rc"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            q.unlink(missing_ok=True)
            continue
        ok, why = may_stop(p, rc, m.get("caller"))
        if ok:
            t = task_of(p, rc)
            r = end(p, rc, f"stopped by `ttp detach --stop` ({why})", t["id"] if t else None)
            if r:
                out.append(r)
        q.unlink(missing_ok=True)
    return out


# Pid files --------------------------------------------------------------------------------------
# `ttp detach --stop-pids <file>`: a task stops the processes its own driver listed in a pid file, one pid
# per line (the line's first word; `start=<token>`, the token runner.proc_start gives, pins it to that
# process; other words and `#` lines are ignored). plan_pids allows it only when all of these hold:
# - the file's resolved path (symlinks followed) lies under one of the calling task's own results dirs: a
#   run dir of the task, or a folder named for it (t<id>, task<id> or task-<id>) inside the project root;
# - each live pid in it runs as the caller's user, carries the task's own TTP_TASK and this project's
#   TTP_PROJECT in its environment (so it was started from one of the task's runs), and is still the
#   process the file recorded: its start= token matches, else it started no later than the file was
#   last written (a later one reuses the pid);
# - none is the caller, one of its ancestors or a running worker, and none holds a lock slot for another
#   task or holder.
# A pid that is gone is skipped; any other refusal refuses the whole file, so nothing is stopped in part.
# stop_pids then sends SIGTERM, waits PIDS_TERM_S and sends SIGKILL to those still the same process.

PIDS_TERM_S = 10.0
PIDS_MAX = 256
TASK_DIR_RE = re.compile(r"(?:t|task-?)(\d+)")


def task_results(p: Project, task: int, f: Path) -> bool:
    """Whether the resolved path f lies under one of the task's results dirs (see above)."""
    for r in p.db.q("SELECT dir FROM runs WHERE task=? AND COALESCE(dir,'')!=''", (task,)):
        if f.is_relative_to(Path(r["dir"]).resolve()):
            return True
    root = p.root.resolve()
    if not f.is_relative_to(root):
        return False
    return any((m := TASK_DIR_RE.fullmatch(x)) and int(m[1]) == task for x in f.relative_to(root).parts[:-1])


def read_pidfile(f: Path) -> list[tuple[int, str | None]]:
    """(pid, start token or None) per line; ValueError for a line whose first word is not a pid."""
    out = []
    for line in f.read_text(errors="replace")[:65536].splitlines():
        w = line.split()
        if not w or w[0].startswith("#"):
            continue
        if not w[0].isdigit():
            raise ValueError(f"line {line.strip()[:80]!r} does not start with a pid")
        out.append((int(w[0]), next((x.split("=", 1)[1] for x in w[1:] if x.startswith("start=")), None)))
    return out


def _environ(pid: int) -> dict[str, str] | None:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    return dict(x.decode(errors="replace").split("=", 1) for x in raw.split(b"\0") if b"=" in x)


def _started_at(pid: int) -> float | None:
    """When pid started, in seconds since the epoch (Linux /proc), else None."""
    from .runner import proc_start
    try:
        btime = next(float(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines() if x.startswith("btime"))
        return btime + int(proc_start(pid) or "") / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration):
        return None


def _guarded(p: Project) -> set[int]:
    """The caller and its ancestors, and every running worker: never a stop target."""
    out, pid = set(), os.getpid()
    while pid > 1 and pid not in out:
        out.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
    out |= {int(r["pid"]) for r in p.db.q("SELECT pid FROM runs WHERE status='running' AND pid IS NOT NULL")}
    return out


def _foreign_slot(p: Project, pid: int, task: int) -> str | None:
    """The name of a held lock slot whose holder process is pid and whose holder is not this task."""
    from . import hold
    node = os.uname().nodename
    for d in lock_dirs(p):
        try:
            slots = sorted(d.glob("*.lock"))
        except OSError:
            continue
        for s in slots:
            try:
                h = json.loads(s.read_text() or "{}")
            except (OSError, ValueError):
                continue
            if (isinstance(h, dict) and h.get("pid") == pid and h.get("node") in (None, node)
                    and hold.task_of(h.get("holder"), p.name) != task and not locks.any_free([s])):
                return s.name
    return None


def pid_refusal(p: Project, task: int, pid: int, token: str | None, written: float, guarded: set[int]) -> str | None:
    """Why pid (live) may not be stopped for this task, else None (see above)."""
    from .runner import proc_start
    if pid <= 1 or pid in guarded:
        return "is the caller, one of its ancestors or a running worker"
    try:
        if Path(f"/proc/{pid}").stat().st_uid != os.getuid():
            return "runs as another user"
    except OSError:
        return "cannot be read"
    env = _environ(pid)
    if env is None:
        return "its environment cannot be read, so it cannot be told to be this task's"
    there = env.get("TTP_PROJECT") or ""
    if not there or Path(there).resolve() != p.base.resolve():
        return "was not started by this project" + (f" (TTP_PROJECT {there})" if there else "")
    if (env.get("TTP_TASK") or env.get("TTP_TASK_ID") or "") != str(task):
        return f"belongs to task #{env.get('TTP_TASK') or '?'}, not #{task}"
    if token is not None:
        if proc_start(pid) != token:
            return f"is not the process the file recorded (start token {token}): the pid was reused"
    else:
        at = _started_at(pid)
        if at is None or at > written + 1:
            return "started after the pid file was last written: the pid was reused"
    if slot := _foreign_slot(p, pid, task):
        return f"holds lock slot {slot} for another holder"
    return None


def plan_pids(p: Project, pidfile: str | Path, caller: str | None,
              procs: bool = True) -> tuple[list[tuple[int, str]], list[int], str | None]:
    """(targets as (pid, start token), pids already gone, refusal or None) for a stop of the pid file by
    task `caller`. procs=False checks only the caller and the file (a sandboxed caller cannot see them)."""
    if not (caller and str(caller).isdigit() and p.db.task(int(caller))):
        return [], [], "only a task of this project may stop processes from a pid file (TTP_TASK is not one)"
    task = int(caller)
    f = Path(pidfile).expanduser()
    try:
        real = f.resolve(strict=True)
        written = real.stat().st_mtime
    except (OSError, RuntimeError):
        return [], [], f"no pid file at {f}"
    if not real.is_file():
        return [], [], f"{real} is not a file"
    if not task_results(p, task, real):
        return [], [], (f"{real} is not under a results dir of task #{task} (one of its run dirs, or a folder "
                        f"t{task}, task{task} or task-{task} inside the project root {p.root})")
    try:
        listed = read_pidfile(real)
    except (OSError, ValueError) as e:
        return [], [], f"{real}: {e}"
    if len(listed) > PIDS_MAX:
        return [], [], f"{real} lists {len(listed)} pids, over {PIDS_MAX}"
    if not procs:
        return [(pid, tok or "") for pid, tok in listed], [], None
    from .runner import proc_start
    guarded, targets, gone, bad = _guarded(p), [], [], []
    for pid, tok in dict.fromkeys(listed):
        st = _stat(pid) if pid > 0 else None
        if st is None:
            gone.append(pid)
            continue
        if why := pid_refusal(p, task, pid, tok, written, guarded):
            bad.append(f"pid {pid} {why}")
        elif (now := proc_start(pid)) is not None:
            targets.append((pid, now))
        else:
            gone.append(pid)
    if bad:
        return [], gone, "refused, nothing stopped: " + "; ".join(bad)
    return targets, gone, None


def stop_pids(p: Project, pidfile: str | Path, task: int, targets: list[tuple[int, str]], gone: list[int],
              why: str) -> dict:
    """SIGTERM each target that is still the same process, SIGKILL those left after PIDS_TERM_S; one event."""
    from .runner import proc_start

    def same(pid, tok):
        return _stat(pid) is not None and proc_start(pid) == tok

    for sig, wait in ((signal.SIGTERM, PIDS_TERM_S), (signal.SIGKILL, FREE_WAIT_S)):
        sent = []
        for pid, tok in targets:
            if same(pid, tok):
                try:
                    os.kill(pid, sig)
                    sent.append((pid, tok))
                except OSError:
                    pass
        deadline = time.monotonic() + wait
        while any(same(*t) for t in sent) and time.monotonic() < deadline:
            time.sleep(0.1)
        if sig == signal.SIGTERM:
            killed = [pid for pid, tok in targets if same(pid, tok)]
    left = [pid for pid, tok in targets if same(pid, tok)]
    pids = [pid for pid, _ in targets]
    text = (f"stopped the processes of pid file {pidfile} (task #{task}): {why}; "
            f"SIGTERM to {', '.join(map(str, pids)) or 'none'}"
            + (f"; SIGKILL after {PIDS_TERM_S:g}s to {', '.join(map(str, killed))}" if killed else "")
            + (f"; already gone: {', '.join(map(str, gone))}" if gone else "")
            + (f"; still running: {', '.join(map(str, left))}" if left else ""))
    out = {"task": task, "job": Path(pidfile).name, "pidfile": str(pidfile), "pids": pids, "killed": killed,
           "gone": gone, "left": left, "text": text}
    if pids:
        fp = "pids_stopped:" + hashlib.sha256(f"{pidfile}:{targets}".encode()).hexdigest()[:16]
        p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
               (time.time(), "daemon", EVENT, fp, "low", text, json.dumps(out), "handled", task))
    return out


def request_pids(p: Project, pidfile: Path, caller: str) -> Path:
    """Queue a pid file stop for the daemon (a sandboxed caller cannot see or signal the processes)."""
    key = hashlib.sha256(str(pidfile).encode()).hexdigest()[:12]
    q = p.state / QUEUE / f"pids-{caller}-{key}.json"
    q.parent.mkdir(parents=True, exist_ok=True)
    write_json(q, {"pidfile": str(pidfile), "caller": caller, "at": time.time()})
    return q


def _serve_pids(p: Project, m: dict) -> list[dict]:
    """The daemon's side of request_pids: check again, stop, and leave the outcome beside the pid file
    (<file>.stopped) for the task to read."""
    f, caller = Path(m["pidfile"]), str(m.get("caller") or "")
    targets, gone, why = plan_pids(p, f, caller)
    r = None if why else stop_pids(p, f, int(caller), targets, gone, "stopped by `ttp detach --stop-pids` (queued)")
    try:   # only into the task's own results dir, never through a link out of it
        out = f.resolve().with_name(f.name + ".stopped")
        if caller.isdigit() and task_results(p, int(caller), out) and not out.is_symlink():
            durable_write(out, f"{r['text'] if r else why}\n")
    except (OSError, RuntimeError):
        pass
    return [r] if r and r["pids"] else []
