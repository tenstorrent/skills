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

A job counts as running while its job lock is held, whatever its .rc or .lost say: a daemon that dies
mid-stop finds it still running on its next tick and finishes the stop; a stopped one is skipped and
logs nothing new.

`ttp detach --stop <rc>` is the manual form: allowed for a job whose task has ended (cancelled, failed,
exhausted or done), or for an open priority-1 caller task (TTP_TASK) whose resources include one the
job's held slots hold. A sandboxed caller, which cannot signal the job, queues the request (QUEUE) and
the daemon carries it out on its next tick, checking it again.
"""
from __future__ import annotations

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
from .project import Project, write_json

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
    `<run>/<name>` as `ttp detach --check <dir>/<name>` writes it (a longer name such as <name>2 is not it)."""
    rc = Path(rc)
    text = " ".join(str(result.get(k) or "") for k in ("retry_when", "on_pass_cmd", "next_step"))
    text += " " + json.dumps(result.get("on_pass") or "")
    tail = re.escape(f"{rc.parent.name}/{rc.stem}")
    return re.search(rf"(?<![\w.-]){tail}(?:\.rc)?(?![\w.-])", text) is not None


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
            rc = Path(m["rc"])
        except (OSError, ValueError, KeyError, TypeError):
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
