# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Checks that overflow from the account's test pool to a Slurm cluster: account `checks.slurm_overflow`.

Unset (the default), nothing changes. Set (`ttp config --account checks.slurm_overflow '{"host":
"<cluster-login>", "partition": "<cpu-partition>", "workdir": "~/ttp-checks"}'`; only the user sets it,
and a run may only clear it, removing it ('') or setting it off ({}), logged: cli._log_account_change), a `ttp checks` run that may spill (checkspill.spillable) and whose bounded wait here ended with no
pool host able to take it (each paused, busy with a device job, at its cap or unreachable; or no pool
at all) is sent to the cluster as one CPU-only batch job per check, instead of waiting on here. A
project whose own delivery.checks_spill_hosts is [] keeps its runs here, the overflow too.

Fields: `host` the ssh alias of the cluster's submit host; `partition` a CPU partition; `workdir` a
folder on the cluster's shared file system (absolute, or under ~ or $HOME; default DEFAULT_WORKDIR);
`max_jobs` this machine's overflow jobs at once (default 2); `margin_s` added to the run's median wall
time for `--time` (default 600); `max_time_s` the cap of `--time` (default 7200), also its value before
a passing run's time is known; `max_pending_s` how long a job may wait to start (default 1800).

Order: a run takes an overflow slot only when no live overflow waiter of this machine ranks before it,
by the checks queue's rank (checkcap.task_rank: task priority, an urgent or obligation run first; then
arrival), so urgent runs go first. A cluster that ssh does not reach, or whose host lacks sbatch, is
skipped and the run keeps its place in the queue here, never failed.

A job (main, run by push.check_argv in the run's place): the commit is copied as remotecheck copies it
(`git archive` over ssh), here into a tar file in a fresh ttp-check.* folder under `workdir`, with the
job script beside it; on the submit host only sbatch, squeue, sacct and scancel run, and the result
files (its output, its exit code) are read. The job itself, on a compute node, unpacks the tree into a
git checkout and runs the command. `sbatch --partition <partition> --time <minutes> --nodes 1 --ntasks 1`,
no device or GRES request. Its output streams back as it grows. A stop of the run (a stop, a timeout,
a newer head) cancels the job (scancel); a job whose run died anyway is cancelled by the next overflow
pick (reap). With the submit host lost mid-job the job still ends at its time limit; the run gives up
after LOST_S. A job that ends for the cluster's reasons (never started within max_pending_s, a node
fault, preempted, cancelled by others, the node unable to unpack the tree) is submitted again once;
then, and for a job past its time limit, the check exits remotecheck.FAULT_RC: as for a spill host,
a run that did not pass there runs again here, in its place in the queue, and only that verdict counts.

Start-failure breaker: a job that dies at start (Slurm ends it, not completed, within START_FAIL_S of its
start and with no output) counts; TRIP_AFTER in a row trip the overflow (BREAKER: when, how many, the last
jobs' ids, states and exit codes). Tripped, it submits no job: pick() keeps runs waiting here in their place
and a job's run that would resubmit faults instead, so the check runs again here and only that verdict counts.
One daemon alerts it once (alert_due, ALERT_KEY), and it clears once the user sets the key again (or clears
it): only that re-arms it, never a timer. A job that completes or gives output resets the count.

Every job is logged in JOBS_LOG (job id, project, task, check, submit, start and end times, state, exit
code) beside the spill pool's claims, and `ttp status` shows the running ones (status_lines)."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import remotecheck

KEY = "checks.slurm_overflow"
DEFAULT_WORKDIR = "~/ttp-checks"
DEFAULTS = {"max_jobs": 2, "margin_s": 600, "max_time_s": 7200, "max_pending_s": 1800}
_FIELDS = {"host", "partition", "workdir", *DEFAULTS}
_PARTITION = re.compile(r"[A-Za-z0-9_.,-]{1,128}")
_WORKDIR = re.compile(r"(/|~/|\$HOME/)[A-Za-z0-9_./-]*|~|\$HOME")
JOBS_LOG = "jobs.jsonl"
JOBS_LOG_MAX_BYTES = 256 * 1024
PROBE_TIMEOUT_S = 30
CALL_TIMEOUT_S = 60
SCANCEL_TIMEOUT_S = 4   # a stopped check is killed 5 s after its TERM (cli._end_check)
POLL_S = 15.0
POLL_ENV = "TTP_SLURM_POLL_S"   # tests poll faster
LOST_S = 600.0   # the submit host unanswered this long mid-job: the run gives up (the job ends at its limit)
WANT_TTL_S = 600.0   # a waiter's ticket not renewed this long is gone (its run took a slot here or died)
CLUSTER_STATES = {"NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "CANCELLED", "REVOKED", "DEADLINE", "FAILED",
                  "OUT_OF_MEMORY", "SPECIAL_EXIT", "STOPPED", "SUSPENDED"}
_ACTIVE = {"PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "REQUEUED", "RESIZING", "SUSPENDED", "STAGE_OUT"}
START_FAIL_S = 30   # a job Slurm ends, not completed, this soon after it started and with no output: died at start
TRIP_AFTER = 3     # that many in a row pause the overflow (tripped) until the user sets the key again
BREAKER = "breaker.json"   # in slurm_dir(): the start-failure count and the trip (_breaker)
ALERT_KEY = "slurm-paused"   # the trip's alert condition (alerts.holds: while tripped() holds)
_BOOT = "import sys; sys.path.insert(0, sys.argv[1]); from ttp.slurmcheck import main; sys.exit(main(sys.argv[2:]))"


def settings_of(v: Any) -> dict:
    """A `checks.slurm_overflow` value (a table, or its JSON text) with defaults filled in. ValueError
    names what is wrong; nothing is guessed."""
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            v = json.loads(v)
        except ValueError:
            pass
    if not isinstance(v, dict):
        raise ValueError(f"{KEY}: {v!r} is not a table such as "
                         '{"host": "<cluster-login>", "partition": "<cpu-partition>"}')
    bad = [f"unknown field {k!r}" for k in v if k not in _FIELDS]
    host, part = v.get("host"), v.get("partition")
    if not isinstance(host, str) or remotecheck.host_problem(host) or host == remotecheck.LOCAL:
        bad.append(f"host {host!r} is not an ssh alias")
    if not isinstance(part, str) or not _PARTITION.fullmatch(part):
        bad.append(f"partition {part!r} is not a partition name")
    work = v.get("workdir", DEFAULT_WORKDIR)
    if not isinstance(work, str) or not _WORKDIR.fullmatch(work) or ".." in work.split("/"):
        bad.append(f"workdir {work!r} is not an absolute folder or one under ~ or $HOME")
    out = {"host": host, "partition": part, "workdir": work.rstrip("/") or "/"}
    for k, d in DEFAULTS.items():
        n = v.get(k, d)
        if isinstance(n, bool) or not isinstance(n, (int, float)) or n < (1 if k == "max_jobs" else 0):
            bad.append(f"{k} {n!r} is not a number of {'jobs' if k == 'max_jobs' else 'seconds'}")
        else:
            out[k] = int(n)
    if not bad and out["max_time_s"] < 60:
        bad.append("max_time_s is under a minute")
    if bad:
        raise ValueError(f"{KEY}: " + "; ".join(bad))
    return out


def settings() -> dict | None:
    """The account's overflow settings, or None when unset or malformed (problems names it)."""
    from . import project
    v = (project.load_account_settings().get("checks") or {}).get("slurm_overflow")
    if v in (None, "", {}):
        return None
    try:
        return settings_of(v)
    except ValueError:
        return None


def problems() -> list[str]:
    from . import project
    v = (project.load_account_settings().get("checks") or {}).get("slurm_overflow")
    if v in (None, "", {}):
        return []
    try:
        settings_of(v)
        return []
    except ValueError as e:
        return [str(e)]


def enabled(cfg: dict | None) -> dict | None:
    """The settings when the project's runs may overflow: set, and the project's own spill list not []."""
    own = ((cfg or {}).get("delivery") or {}).get("checks_spill_hosts")
    if own is not None and not own:
        return None
    return settings()


def time_min(median_s: float | None, s: dict) -> int:
    """The job's `--time` in minutes: the run's median wall time plus the margin, within max_time_s;
    max_time_s itself before a median is known."""
    t = s["max_time_s"] if median_s is None else min(s["max_time_s"], float(median_s) + s["margin_s"])
    return max(1, math.ceil(t / 60))


def slurm_dir() -> Path:
    from . import checkspill
    return checkspill.claim_dir().parent / "slurm"


def _alive(pid: Any) -> bool:
    from .liveness import pid_state, DEAD
    return pid_state(pid) != DEAD


def _read(f: Path) -> dict:
    try:
        r = json.loads(f.read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return r if isinstance(r, dict) else {}


def _rank(prio: Any, arrived: Any) -> tuple:
    return (prio if isinstance(prio, int) else 1 << 30, float(arrived or 0))


def ahead(prio: int | None, arrived: float, now: float | None = None) -> int:
    """This machine's live overflow waiters that rank before (prio, arrived), other than this process."""
    now = time.time() if now is None else now
    d = slurm_dir()
    n = 0
    for f in d.glob("want.*.json") if d.is_dir() else []:
        r = _read(f)
        if r.get("pid") == os.getpid() or not _alive(r.get("pid")) or float(r.get("until") or 0) < now:
            continue
        if _rank(r.get("prio"), r.get("arrived")) < _rank(prio, arrived):
            n += 1
    return n


def want(prio: int | None, arrived: float, project_name: str) -> None:
    """Keep (or renew) this process's place among the overflow waiters."""
    from .project import write_json
    d = slurm_dir()
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / f"want.{os.getpid()}.json", {"pid": os.getpid(), "prio": prio, "arrived": arrived,
                                               "project": project_name, "until": time.time() + WANT_TTL_S})


def forget() -> None:
    """This process waits for the overflow no more."""
    (slurm_dir() / f"want.{os.getpid()}.json").unlink(missing_ok=True)


def _ssh(host: str, script: str, timeout: float, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(remotecheck._ssh(host, script), input=data, stdin=None if data is not None else
                          subprocess.DEVNULL, capture_output=True, timeout=timeout)


def reachable(host: str) -> str:
    """"" when ssh reaches the submit host and it has sbatch and squeue, else why not."""
    try:
        r = _ssh(host, "command -v sbatch >/dev/null && command -v squeue >/dev/null || exit 3", PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"ssh timed out after {PROBE_TIMEOUT_S} s"
    except OSError as e:
        return f"cannot run ssh: {e}"
    if r.returncode == 3:
        return "it lacks sbatch or squeue"
    if r.returncode:
        return f"ssh cannot reach it: {(r.stderr.decode(errors='replace').strip().splitlines() or [f'exit {r.returncode}'])[-1][:200]}"
    return ""


def scancel(host: str, job: str) -> bool:
    try:
        return _ssh(host, f"scancel {shlex.quote(job)}", SCANCEL_TIMEOUT_S).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def log_job(rec: dict) -> None:
    from .project import durable_append, durable_write
    path = slurm_dir() / JOBS_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > JOBS_LOG_MAX_BYTES:
            durable_write(path, "".join(f"{x}\n" for x in path.read_text().splitlines()[-200:]))
        durable_append(path, json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass


def jobs(project_name: str | None = None) -> list[dict]:
    """The logged jobs, oldest first (of `project_name` when given)."""
    out = []
    try:
        lines = (slurm_dir() / JOBS_LOG).read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and (project_name is None or r.get("project") == project_name):
            out.append(r)
    return out


def reap() -> list[str]:
    """Cancel the jobs whose run died without ending them (a SIGKILL, a reboot): the job ids cancelled."""
    d = slurm_dir()
    out = []
    for f in sorted(d.glob("job.*.json")) if d.is_dir() else []:
        r = _read(f)
        if not r or _alive(r.get("pid")):
            continue
        if r.get("job") and r.get("host") and scancel(r["host"], str(r["job"])):
            out.append(str(r["job"]))
        log_job({**r, "end": time.time(), "state": "ORPHANED", "note": "its run died; cancelled"})
        f.unlink(missing_ok=True)
    return out


class Claim:
    """This machine's hold of one overflow slot (as checkspill.Claim, with `slurm` the settings)."""

    def __init__(self, host: str, f, s: dict):
        self.host, self.f, self.slurm, self.lock = host, f, s, None

    def close(self) -> None:
        if self.f is not None:
            try:
                self.f.truncate(0)
            except OSError:
                pass
            self.f.close()
            self.f = None


def _claim(s: dict, rec: dict) -> Claim | None:
    d = slurm_dir()
    d.mkdir(parents=True, exist_ok=True)
    for i in range(s["max_jobs"]):
        f = open(d / f"{s['host']}.{i}.json", "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            continue
        f.seek(0)
        f.truncate(0)
        f.write(json.dumps(rec))
        f.flush()
        return Claim(s["host"], f, s)
    return None


def pick(p, cfg: dict, tell, what: str = "", prio: int | None = None, arrived: float | None = None) -> Claim | None:
    """An overflow slot on the cluster, claimed, or None (unset, others rank first, all taken, the
    cluster unreachable): the run then keeps its place in the queue here. Called only once no pool
    host could take the run."""
    s = enabled(cfg)
    if s is None:
        return None
    if trip := tripped():
        tell(f"no pool host could take the checks and the Slurm overflow ({s['host']}) paused itself: "
             f"{trip_why(trip)}; waiting here")
        return None
    arrived = time.time() if arrived is None else arrived
    reap()
    want(prio, arrived, p.name)
    if n := ahead(prio, arrived):
        tell(f"no pool host could take the checks; {n} run(s) ahead of this one wait for the Slurm overflow")
        return None
    rec = {"project": p.name, "task": os.environ.get("TTP_TASK") or "", "run": os.environ.get("TTP_RUN_ID") or "",
           "pid": os.getpid(), "since": time.time(), "what": what[:300], "host": s["host"]}
    got = _claim(s, rec)
    if got is None:
        tell(f"no pool host could take the checks and the Slurm overflow ({s['host']}) runs its "
             f"{s['max_jobs']} job(s) already: waiting here")
        return None
    if why := remotecheck.paused(s["host"]) or reachable(s["host"]):
        got.close()
        tell(f"no pool host could take the checks and the Slurm overflow ({s['host']}) cannot now: {why}; "
             "waiting here")
        return None
    forget()
    tell(f"no pool host could take the checks: overflowing to Slurm cluster {s['host']} (partition "
         f"{s['partition']}, {KEY}); each check runs there as one batch job on a copy of the commit")
    return got


def holders(project_name: str | None = None) -> list[dict]:
    d = slurm_dir()
    out = []
    for f in sorted(d.glob("*.*.json")) if d.is_dir() else []:
        if f.name.startswith(("want.", "job.")):
            continue
        r = _read(f)
        if r.get("host") and _alive(r.get("pid")) and (project_name is None or r.get("project") == project_name):
            out.append(r)
    return out


def status_lines(project_name: str, now: float | None = None) -> list[str]:
    """`ttp status` lines: this project's checks running as Slurm overflow jobs, with their job ids."""
    now = time.time() if now is None else now
    d = slurm_dir()
    live = [r for f in (sorted(d.glob("job.*.json")) if d.is_dir() else []) if (r := _read(f))
            and r.get("project") == project_name and _alive(r.get("pid"))]
    out = []
    for r in holders(project_name):
        who = f"task #{r['task']}" if r.get("task") else f"pid {r.get('pid')}"
        mine = [j for j in live if j.get("task") == r.get("task") and j.get("run") == r.get("run")]
        mins = max(0, int((now - float(r.get("since") or now)) // 60))
        out.append(f"checks of {who} running on Slurm cluster {r['host']} for {mins} min"
                   + "".join(f", job {j.get('job') or '?'} {j.get('state') or 'SUBMITTING'}" for j in mine)
                   + (f": {r['what']}" if r.get("what") else ""))
    return out


def _raw() -> Any:
    from . import project
    return (project.load_account_settings().get("checks") or {}).get("slurm_overflow")


def _digest(v: Any) -> str:
    return json.dumps(v, sort_keys=True)


def _set_since(key_ts: float) -> bool:
    """Whether the key was set (or cleared) again after `key_ts`, by the account settings log."""
    from . import accountset
    rec = accountset.last(KEY)
    return bool(rec) and float(rec.get("ts") or 0) > key_ts


def _armed(b: dict) -> bool:
    """Whether breaker record `b` is stale: the key's value changed, or it was set again, since it began."""
    return _digest(_raw()) != b.get("value") or _set_since(float(b.get("since") or 0))


def tripped() -> dict | None:
    """The trip record while the overflow paused itself (TRIP_AFTER jobs in a row died at start) and the
    user has not set checks.slurm_overflow again since; else None."""
    b = _read(slurm_dir() / BREAKER)
    return b if b.get("tripped") and not _armed(b) else None


def trip_why(b: dict) -> str:
    last = (b.get("jobs") or [{}])[-1]
    return (f"its last {b.get('count')} jobs ended at start with no output (last: job {last.get('job') or '?'} "
            f"{last.get('state') or '?'}, exit code {last.get('code') or '?'}, {last.get('elapsed_s', '?')} s)")


def _breaker(start_fail: dict | None, now: float | None = None) -> dict | None:
    """Count one overflow job's end: `start_fail` (the job's id, state, exit code, elapsed seconds) for a job
    that died at start, None for one that ran (completed, or gave output). Returns the record when this
    end tripped the overflow (the caller tells it), else None. A record from before the key's last
    change starts over: the user's setting it again re-arms the overflow."""
    from .project import write_json
    now = time.time() if now is None else now
    d = slurm_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / (BREAKER + ".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        b = _read(d / BREAKER)
        if not b or _armed(b):
            b = {"since": now, "value": _digest(_raw()), "count": 0, "jobs": []}
        if b.get("tripped"):
            return None   # paused: only the user's setting re-arms it, never a job that still ended
        if start_fail is None:
            if b["count"]:
                b.update(count=0, jobs=[])
                write_json(d / BREAKER, b)
            return None
        b["count"] += 1
        b["jobs"] = [*b.get("jobs", []), {**start_fail, "end": now}][-TRIP_AFTER:]
        if b["count"] >= TRIP_AFTER:
            b["tripped"] = now
        write_json(d / BREAKER, b)
        return b if b.get("tripped") else None


def alert_due(project_name: str) -> dict | None:
    """The trip record for the one project that alerts it (the first daemon to ask), once per trip."""
    from .project import write_json
    d = slurm_dir()
    if not (d / BREAKER).exists():
        return None
    with open(d / (BREAKER + ".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        b = tripped()
        if not b or b.get("alerted"):
            return None
        b["alerted"] = project_name
        write_json(d / BREAKER, b)
        return b


def alert_text(b: dict) -> str:
    return (f"The Slurm checks overflow ({KEY}) paused itself: {trip_why(b)}. It submits no more jobs; "
            "check runs wait on the local queue in their place, and none counts as a failed check. Find why "
            f"the jobs die (`sacct -j <job>` on the cluster), then set {KEY} again to re-arm it, or clear it.")


def paused_line(now: float | None = None) -> str:
    """One status line while the overflow paused itself, else ""."""
    if not (b := tripped()):
        return ""
    now = time.time() if now is None else now
    mins = max(0, int((now - float(b["tripped"])) // 60))
    return (f"Slurm checks overflow paused itself {mins} min ago: {trip_why(b)}; runs stay on the local queue; "
            f"setting {KEY} again re-arms it")


def argv(cmd: str, s: dict, env: dict | None = None, commit: str = "", minutes: int = 0) -> list[str]:
    """The local command that runs check `cmd` as one job on the cluster (main)."""
    return [sys.executable, "-c", _BOOT, str(Path(__file__).resolve().parents[1]),
            "--settings", json.dumps(s, sort_keys=True), "--time-min", str(minutes or time_min(None, s)),
            "--env", json.dumps(env or {}, sort_keys=True), *(["--commit", commit] if commit else []), "--", cmd]


def job_script(q: str, exports_: str, cmd: str) -> str:
    """The batch script, run on a compute node in folder `q`: unpack the tree into a git checkout, run
    the command, leave its exit code in `rc` ("fault" when the node could not set the tree up)."""
    return ("#!/bin/sh\nrc=fault\n"
            f"trap 'printf \"%s\\n\" \"$rc\" > {q}/rc.part && mv -f {q}/rc.part {q}/rc' EXIT\n"
            f"cd {q} || exit 1\nrm -rf src && mkdir src && tar -xf tree.tar -C src && cd src && "
            "git -c init.defaultBranch=main init -q && git add -A -f . && "
            "git -c user.name=ttp -c user.email=ttp@localhost -c commit.gpgsign=false commit -q --no-verify "
            "--allow-empty -m 'ttp check' || exit 1\n"
            f"{exports_}rc=killed\n"
            f"if command -v bash >/dev/null 2>&1; then bash -e -o pipefail -c {shlex.quote(cmd)} </dev/null; "
            f"else sh -e -c {shlex.quote(cmd)} </dev/null; fi\nrc=$?\nexit 0\n")


def _say(msg: str) -> None:
    sys.stdout.write(f"ttp slurm check: {msg}\n")
    sys.stdout.flush()


def _fault(host: str, why: str) -> int:
    _say(f"environment fault {remotecheck.SIG}{host}: {why}. This is the cluster, not the change")
    return remotecheck.FAULT_RC


class _Stop(Exception):
    pass


def _stop(_sig, _frame) -> None:
    raise _Stop()


def _sacct(host: str, job: str) -> tuple[str, str, int | None]:
    """(state, exit code, elapsed seconds or None) of a finished job; ("", "", None) when sacct does not
    know it (yet)."""
    try:
        r = _ssh(host, f"sacct -n -X -P -j {shlex.quote(job)} -o State,ExitCode,ElapsedRaw", CALL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return "", "", None
    line = (r.stdout.decode(errors="replace").strip().splitlines() or [""])[0]
    state, code, elapsed = (line.split("|") + ["", ""])[:3]
    return (state.split() or [""])[0].upper(), code.strip(), int(elapsed) if elapsed.strip().isdigit() else None


def _start_fail(job: str, state: str, why: str, seen: dict) -> dict | None:
    """The job's record when it died at start (Slurm ended it, not completed, within START_FAIL_S of its
    start, with no output), else None. A job cancelled here for pending too long did not start: neither."""
    if not why.startswith("cluster") or state in ("COMPLETED", "PENDING") or seen.get("out_bytes"):
        return None
    elapsed = seen.get("elapsed_s")
    if elapsed is not None and elapsed >= START_FAIL_S:
        return None
    return {"job": job, "state": state, "code": seen.get("code") or "", "elapsed_s": elapsed if elapsed is not None else 0}


def main(args: list[str]) -> int:
    s, env, commit, minutes = {}, {}, "", 0
    while args and args[0] != "--":
        flag, value, args = args[0], args[1] if len(args) > 1 else "", args[2:]
        if flag == "--settings":
            s = json.loads(value or "{}")
        elif flag == "--env":
            env = json.loads(value or "{}")
        elif flag == "--commit":
            commit = value
        elif flag == "--time-min":
            minutes = int(value or 0)
    cmd = " ".join(args[1:])
    try:
        s = settings_of(s)
    except ValueError as e:
        _say(f"usage: --settings <{KEY}> [--time-min <n>] [--env <json>] -- <command> ({e})")
        return 2
    if remotecheck.env_problem(env) or not cmd:
        _say(f"usage: --settings <{KEY}> [--time-min <n>] [--env <json>] -- <command>")
        return 2
    host = s["host"]
    if why := remotecheck.paused(host):
        return _fault(host, why)
    if trip := tripped():
        return _fault(host, f"the Slurm overflow paused itself: {trip_why(trip)}")
    top = remotecheck._git("rev-parse", "--show-toplevel")
    head = "" if commit.startswith("-") else remotecheck._git("rev-parse", "--verify", "-q", f"{commit or 'HEAD'}^{{commit}}")
    if not top or not head:
        _say("no such commit in this checkout; nothing to copy")
        return 2
    minutes = minutes or time_min(None, s)
    poll = float(os.environ.get(POLL_ENV) or POLL_S)
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _stop)
    rec_path = slurm_dir() / f"job.{os.getpid()}.json"
    rec = {"project": "", "task": os.environ.get("TTP_TASK") or "", "run": os.environ.get("TTP_RUN_ID") or "",
           "pid": os.getpid(), "host": host, "partition": s["partition"], "check": cmd[:300], "head": head,
           "time_min": minutes}
    try:
        from . import project
        p = project.here(top)
        rec["project"] = p.name if p else ""
    except Exception:
        pass
    tmp, job, active = "", "", False

    def note(**kw) -> None:
        from .project import write_json
        rec.update(kw)
        try:
            rec_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(rec_path, rec)
        except OSError:
            pass

    try:
        w = s["workdir"]
        wq = w if w in ("~", "$HOME") or w.startswith(("~/", "$HOME/")) else shlex.quote(w)
        try:
            made = _ssh(host, f'mkdir -p {wq} && mktemp -d {wq}/ttp-check.XXXXXXXX', PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return _fault(host, f"ssh timed out after {PROBE_TIMEOUT_S} s")
        tmp = made.stdout.decode(errors="replace").strip()
        if made.returncode != 0 or not remotecheck._TMP.fullmatch(tmp):
            tmp = ""
            return _fault(host, f"cannot make a folder in {w} there (ssh exit {made.returncode})")
        q = shlex.quote(tmp)
        archive = subprocess.run(["git", "-C", top, "archive", "--format=tar", head], capture_output=True)
        if archive.returncode != 0:
            _say(f"git archive of {head[:12]} failed (exit {archive.returncode})")
            return 2
        script = job_script(q, remotecheck.exports(env), cmd).encode()
        try:
            put = _ssh(host, f"cat > {q}/tree.tar", remotecheck.SETUP_TIMEOUT_S, archive.stdout)
            put2 = _ssh(host, f"cat > {q}/job.sh", CALL_TIMEOUT_S, script) if put.returncode == 0 else put
        except subprocess.TimeoutExpired:
            return _fault(host, "copying the tree timed out")
        if put2.returncode != 0:
            return _fault(host, f"cannot copy the tree there (ssh exit {put2.returncode})")
        sbatch = (f"sbatch --parsable --job-name=ttp-check --partition={shlex.quote(s['partition'])} "
                  f"--time={minutes} --nodes=1 --ntasks=1 --output={q}/out.log --chdir={q} {q}/job.sh")
        for attempt in (1, 2):
            try:
                sub = _ssh(host, f"rm -f {q}/rc {q}/out.log; {sbatch}", CALL_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                return _fault(host, "sbatch timed out")
            job = (sub.stdout.decode(errors="replace").strip().splitlines() or [""])[-1].split(";")[0].strip()
            if sub.returncode != 0 or not job.isdigit():
                job = ""
                err = (sub.stderr.decode(errors="replace").strip().splitlines() or [f"exit {sub.returncode}"])[-1]
                return _fault(host, f"sbatch refused the job: {err[:200]}")
            active, submitted = True, time.time()
            note(job=job, state="PENDING", submit=submitted, attempt=attempt)
            _say(f"checking {head[:12]} as Slurm job {job} on {host} (partition {s['partition']}, "
                 f"--time={minutes} min) in {tmp}")
            rc, state, why, seen = _follow(host, q, job, s, poll, submitted, note)
            active = False
            log_job({**rec, "end": time.time(), "state": state, **({"rc": rc} if rc is not None else {})})
            died = _start_fail(job, state, why, seen) if rc is None else None
            if (died or rc is not None or seen.get("out_bytes") or state in ("COMPLETED", "TIMEOUT")) and (
                    trip := _breaker(died)):
                _say(f"the Slurm overflow paused itself: {trip_why(trip)}; no more jobs go there until "
                     f"{KEY} is set again")
                return _fault(host, f"job {job}: {why}; the overflow paused itself")
            if rc is not None:
                if rc == remotecheck.FAULT_RC:
                    _say(f"the command exited {remotecheck.FAULT_RC}; reported as 1")
                    return 1
                return rc
            if why.startswith("cluster") and attempt == 1 and not tripped():
                _say(f"job {job} ended for the cluster's reasons ({why}): submitting it again once; "
                     "this is no test failure")
                rec.pop("start", None)
                continue
            return _fault(host, f"job {job}: {why}")
        return _fault(host, "the job did not run")
    except _Stop:
        return 143
    finally:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        if active and job:
            ok = scancel(host, job)
            _say(f"stopped: {'cancelled' if ok else 'could not reach the cluster to cancel'} job {job}"
                 + ("" if ok else "; it ends at its time limit"))
            log_job({**rec, "end": time.time(), "state": "CANCELLED" if ok else "CANCEL_FAILED",
                     "note": "the run stopped"})
        rec_path.unlink(missing_ok=True)
        if tmp:
            try:
                _ssh(host, f"rm -rf {shlex.quote(tmp)}", remotecheck.CLEANUP_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError):
                _say(f"could not remove {tmp} on {host}")


def _follow(host: str, q: str, job: str, s: dict, poll: float, submitted: float, note) -> tuple[int | None, str, str, dict]:
    """Wait for `job`, streaming its output: (the command's exit code or None, final state, why it did
    not report one: "cluster: ..." for an end the cluster caused, else the reason, what was seen of it:
    out_bytes, and for an end sacct read its exit code and elapsed_s)."""
    mark = f"ttp-slurm:{secrets.token_hex(6)}"
    sent, last_ok, gone, started = 0, time.time(), 0, None
    out = sys.stdout.buffer
    jq = shlex.quote(job)
    while True:
        script = (f"s=$(squeue -h -j {jq} -o %T 2>/dev/null | head -n 1); r=$(head -n 1 {q}/rc 2>/dev/null); "
                  f"printf '%s|%s|%s\\n' {mark} \"$s\" \"$r\"; tail -c +{sent + 1} {q}/out.log 2>/dev/null; exit 0")
        try:
            got = _ssh(host, script, CALL_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            got = None
        head_line, _, body = (got.stdout if got is not None else b"").partition(b"\n")
        parts = head_line.decode(errors="replace").split("|")
        if got is None or got.returncode != 0 or parts[0] != mark:
            if time.time() - last_ok > LOST_S:
                return None, "UNKNOWN", f"the submit host stopped answering for {LOST_S:g} s; the job ends at its time limit", {"out_bytes": sent}
            time.sleep(poll)
            continue
        last_ok = time.time()
        if body:
            out.write(body)
            out.flush()
            sent += len(body)
        state, rc = (parts[1].strip().upper() if len(parts) > 1 else ""), (parts[2].strip() if len(parts) > 2 else "")
        if state == "RUNNING" and started is None:
            started = time.time()
            note(state=state, start=started)
        if rc.isdigit():
            if state in _ACTIVE:   # the script wrote its code: the job is ending
                time.sleep(min(poll, 2))
            return int(rc), "COMPLETED", "", {"out_bytes": sent}
        if state in _ACTIVE:
            gone = 0
            if state == "PENDING" and started is None and time.time() - submitted > s["max_pending_s"]:
                scancel(host, job)
                return None, "PENDING", f"cluster: it did not start within {s['max_pending_s']} s", {"out_bytes": sent}
            time.sleep(poll)
            continue
        final, code, elapsed = _sacct(host, job)
        if elapsed is None and started is not None:   # sacct gave none: from when it was seen running
            elapsed = round(time.time() - started)
        seen = {"out_bytes": sent, "code": code, "elapsed_s": elapsed}
        if final in _ACTIVE or (not final and gone < 2):
            gone += 1
            time.sleep(poll)
            continue
        if rc == "fault":
            return None, final or "FAILED", "cluster: the node could not set up the tree", seen
        if final == "TIMEOUT":
            return None, final, "it ran past its time limit", seen
        return None, final or "UNKNOWN", f"cluster: it ended {final or 'unseen'} ({code or 'no exit code'}) before the command reported one", seen
