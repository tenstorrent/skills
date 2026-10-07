# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One serial device-job runner per device host: a queue instead of one detached driver per task.

A project's config names each runner under `device.runners.<name>` (see RUNNER_DEFAULTS): the host it
runs on (ssh alias; "" = this machine), its state folder there, a health gate and a drop check. Tasks
queue jobs with `ttp devq submit`, then hand off `waiting` on `ttp devq probe <name> <id>`; the runner
takes the jobs one at a time in arrival order, each only after the health gate passes, and writes a
done marker per job. A job a device drop or a host reboot killed is run again; once one config dropped
max_drops times in a row its jobs are skipped and marked so, until `ttp devq clear`.

This file is also the runner itself: `ttp devq` copies it to the host's state folder and runs it there
with that host's python3, so it uses the standard library only and runs on Linux and macOS alike.
Layout of the state folder: queue/<seq>-<id>.json (waiting, in name order), running/<id>.json and
<id>.state.json (the job under way), done/<id>.json (the marker), logs/<id>.<attempt>.log, configs/<config>
(the config's drops in a row), drops.log, runner.log, state (what the runner does now), runner.lock
(held for the runner's life), config.json (the runner's settings, rewritten by every submit and start).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

RUNNER_DEFAULTS = {
    "host": "",                # ssh alias of the device host; "" = this machine
    "dir": "",                 # state folder on that host; "" = ~/.tt-project/devq/<name>
    "python": "python3",       # the host's python
    "health": "",              # shell command; exit 0 = safe to start a job ("" = no gate)
    "health_timeout_s": 300,   # one health check longer than this counts as a refusal
    "health_poll_s": 60,       # between health checks while the gate refuses
    "health_wait_s": 7200,     # a job the gate kept out this long is skipped (0 = wait for ever)
    "drop_check": "",          # shell command run after a job failed; exit 0 = it was a device drop
    "max_drops": 2,            # drops in a row of one config before its jobs are skipped
    "job_timeout_s": 0,        # default per-job limit (0 = none); a job's own timeout_s wins
    # The box's reservation cap (0 = none): the longest a job may hold the device. Submit refuses a job
    # whose limit (its timeout_s, else job_timeout_s) is above it, a job queued before the cap was set or
    # lowered is failed unstarted, and a job with no limit of its own gets the cap as its limit.
    "reservation_cap_s": 0,
    "idle_exit_s": 1800,       # the runner exits after this long with nothing queued; submit restarts it
    "poll_s": 10,              # between checks on a running job
    # A regex matching the command line of the project's old per-task drivers ("" = none). While one
    # of the user's processes matches, the runner does not start and starts no job, and the probe keeps
    # a task with a pending job asleep: both checking "no other job of ours" at once could pass.
    "legacy_driver": "",
}
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
NAME_RE = ID_RE
STATUSES = ("done", "failed", "skipped")


def config_problems(name: str, cfg) -> list:
    """One line per problem in config device.runners.<name>."""
    where = f"device.runners.{name}"
    if not NAME_RE.fullmatch(str(name)):
        return [f"{where}: a runner name is letters, digits, . _ -"]
    if not isinstance(cfg, dict):
        return [f"{where}: not a table of settings"]
    out = [f"{where}.{k}: unknown key" for k in cfg if k not in RUNNER_DEFAULTS]
    for k, v in cfg.items():
        if k not in RUNNER_DEFAULTS:
            continue
        if isinstance(RUNNER_DEFAULTS[k], int) and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            out.append(f"{where}.{k}: {v!r} is not a number >= 0")
        elif isinstance(RUNNER_DEFAULTS[k], str) and not isinstance(v, str):
            out.append(f"{where}.{k}: {v!r} is not a string")
    if isinstance(cfg.get("legacy_driver"), str) and cfg["legacy_driver"]:
        try:
            re.compile(cfg["legacy_driver"])
        except re.error as e:
            out.append(f"{where}.legacy_driver: not a regex ({e})")
    cap, default = cfg.get("reservation_cap_s"), cfg.get("job_timeout_s")
    if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (cap, default)) and 0 < cap < default:
        out.append(f"{where}.job_timeout_s: {default} is above reservation_cap_s {cap}")
    if isinstance(cfg.get("host"), str) and cfg["host"].startswith("-"):
        out.append(f"{where}.host: must not start with '-'")
    return out


def settings(cfg: dict | None) -> dict:
    return {**RUNNER_DEFAULTS, **{k: v for k, v in (cfg or {}).items() if k in RUNNER_DEFAULTS}}


# project side: how `ttp devq` reaches a runner -------------------------------------------------------
def runner_dir(name: str, cfg: dict) -> str:
    return settings(cfg)["dir"] or f"~/.tt-project/devq/{name}"


def _shell_path(path: str) -> str:
    """`path` as one shell word, a leading ~ still expanded by the host's shell."""
    import shlex
    if path == "~" or path.startswith("~/"):
        return '"$HOME"' + (("/" + shlex.quote(path[2:])) if path[2:] else "")
    return shlex.quote(path)


def host_call(name: str, cfg: dict, op: str, args: list, install: bool) -> list:
    """The argv that runs `devq.py <op> <dir> <args>` on the runner's host, with this file's source on its
    stdin. `install` first copies the source into the state folder (the runner runs from that copy);
    without it the host's python reads the source from stdin and nothing is written by the call."""
    import shlex
    s = settings(cfg)
    d, py = _shell_path(runner_dir(name, s)), shlex.quote(s["python"])
    words = " ".join(shlex.quote(str(x)) for x in args)
    if install:
        script = (f"mkdir -p {d} && cat > {d}/.devq.py.$$ && mv -f {d}/.devq.py.$$ {d}/devq.py && "
                  f"exec {py} {d}/devq.py {op} {d} {words}")
    else:
        script = f"exec {py} - {op} {d} {words}"
    if s["host"]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", s["host"], script]
    return ["/bin/sh", "-c", script]


def source() -> str:
    return Path(__file__).read_text()


# host side: everything below runs on the device host ------------------------------------------------
def job_limit(spec: dict, cfg: dict) -> float:
    """The job's time limit in seconds (0 = none): its timeout_s, else job_timeout_s, else the cap."""
    return float(spec.get("timeout_s") or cfg["job_timeout_s"] or cfg["reservation_cap_s"] or 0)


def over_cap(spec: dict, cfg: dict) -> str:
    """Why the job may not run under the box's reservation cap, or ""."""
    cap, limit = float(cfg["reservation_cap_s"] or 0), job_limit(spec, cfg)
    if cap and limit > cap:
        return (f"its limit of {limit:.0f} s is above this box's reservation cap of {cap:.0f} s; "
                f"submit it again with --timeout {cap:.0f} or less")
    return ""


def _now() -> float:
    return time.time()


def _utc(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(_now() if t is None else t))


def _write(path: Path, text: str) -> None:
    """project.durable_write's standard-library twin (this file runs alone on the device host): a power
    cut leaves the old content or the new, never a half-written file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _sync_dir(path.parent)


def _sync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _append(path: Path, line: str) -> None:
    """Append one line and sync it: the drop counts decide when a config's jobs are skipped."""
    with open(path, "a") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _load(path: Path) -> dict:
    try:
        v = json.loads(path.read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _rm(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _dirs(d: Path) -> None:
    for sub in ("queue", "running", "done", "logs", "configs"):
        (d / sub).mkdir(parents=True, exist_ok=True)


def _free(lock: Path) -> bool:
    """Whether nobody holds this lock file (a missing one is free). Opens it read-only: no writes."""
    try:
        f = open(lock)
    except OSError:
        return True
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        f.close()


def _queued(d: Path, job: str) -> list:
    return sorted(q for q in (d / "queue").glob(f"*-{job}.json") if q.stem.split("-", 1)[1] == job)


def _running(d: Path) -> list:
    return sorted(f.stem for f in (d / "running").glob("*.json") if not f.name.endswith(".state.json"))


def where_is(d: Path, job: str) -> str:
    """done, running, queued or unknown."""
    if (d / "done" / f"{job}.json").exists():
        return "done"
    if (d / "running" / f"{job}.json").exists():
        return "running"
    return "queued" if _queued(d, job) else "unknown"


def runner_alive(d: Path) -> bool:
    return not _free(d / "runner.lock")


def legacy_drivers(d: Path, pattern: str) -> list:
    """This user's processes whose command line matches `pattern`: "<pid> <command>" each. Leaves out
    this process and its ancestors (whose command lines may carry the pattern as config), the runner's
    own processes and its jobs (their process groups)."""
    if not pattern:
        return []
    try:
        rx = re.compile(pattern)
        out = subprocess.run(["ps", "-ww", "-U", str(os.getuid()), "-o", "pid=", "-o", "ppid=", "-o", "pgid=",
                              "-o", "command="], capture_output=True, text=True, timeout=30).stdout
    except (OSError, re.error, subprocess.SubprocessError):
        return []
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and all(x.isdigit() for x in parts[:3]):
            procs[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3])
    mine, pid = set(), os.getpid()
    while pid and pid not in mine:
        mine.add(pid)
        pid = procs.get(pid, (0,))[0]
    jobs = set()
    for st in (d / "running").glob("*.state.json"):
        cur = _load(st).get("cur") or {}
        if isinstance(cur.get("pid"), int):
            jobs.add(cur["pid"])
    return [f"{p} {cmd[:160]}" for p, (_, pgid, cmd) in sorted(procs.items())
            if p not in mine and pgid not in jobs and "devq.py" not in cmd and rx.search(cmd)]


def probe(d: Path, job: str) -> int:
    """retry_when probe, read-only: 0 once job's marker exists, the job is unknown, or it is pending with
    no runner alive (the waking run restarts it with `ttp devq start`); 1 while a live runner has it."""
    at = where_is(d, job)
    if at == "done":
        m = _load(d / "done" / f"{job}.json")
        print(f"{job}: {m.get('status', '?')} rc={m.get('rc')} log={m.get('log', '')}")
        return 0
    if at == "unknown":
        print(f"{job}: unknown to the runner in {d}")
        return 0
    if not runner_alive(d):
        legacy = legacy_drivers(d, settings(_load(d / "config.json"))["legacy_driver"])
        if legacy:
            print(f"{job}: {at}; no runner yet: an old per-task driver still runs ({legacy[0]})")
            return 1
        print(f"{job}: {at}, but no runner is alive: run `ttp devq start` and wait again")
        return 0
    print(f"{job}: {at}")
    return 1


def submit(d: Path, cfg: dict, spec: dict) -> int:
    """Validate and queue one job spec, then start the runner if it is not up. Exit 2 on a bad spec."""
    _dirs(d)
    job = str(spec.get("id") or "")
    if not ID_RE.fullmatch(job):
        print("devq submit: id missing or not letters, digits, . _ - (at most 100)", file=sys.stderr)
        return 2
    spec.setdefault("config", job)
    if not ID_RE.fullmatch(str(spec["config"])):
        print("devq submit: config must be letters, digits, . _ -", file=sys.stderr)
        return 2
    if not isinstance(spec.get("cmd"), str) or not spec["cmd"].strip():
        print("devq submit: cmd missing", file=sys.stderr)
        return 2
    wd = spec.get("workdir") or ""
    if wd and not Path(wd).is_dir():
        print(f"devq submit: workdir {wd} does not exist on this host", file=sys.stderr)
        return 2
    too_long = over_cap(spec, settings(cfg))
    if too_long:
        print(f"devq submit: refused: {too_long}", file=sys.stderr)
        return 2
    if where_is(d, job) != "unknown":
        print(f"devq submit: id {job} was already used here; pick a new one (e.g. {job}-r2)", file=sys.stderr)
        return 2
    spec["submitted"] = _now()
    # The settings go down before the job: an idle runner may dequeue it at once and must see them.
    _write(d / "config.json", json.dumps(settings(cfg), indent=1))
    _write(d / "queue" / f"{time.time_ns():020d}-{job}.json", json.dumps(spec, indent=1))
    ahead = len(list((d / "queue").glob("*.json"))) - 1 + len(_running(d))
    print(f"queued {job} ({ahead} ahead)")
    rc = start(d, cfg)
    return 0 if rc == 3 else rc


def start(d: Path, cfg: dict) -> int:
    """Start the runner unless one is up. Safe to call any number of times."""
    _dirs(d)
    _write(d / "config.json", json.dumps(settings(cfg), indent=1))
    if runner_alive(d):
        print(f"runner already running (pid {(d / 'runner.pid').read_text().strip() if (d / 'runner.pid').exists() else '?'})")
        return 0
    legacy = legacy_drivers(d, settings(cfg)["legacy_driver"])
    if legacy:
        print(f"runner not started: an old per-task driver still runs ({legacy[0]}); the probe keeps the task "
              "asleep until it ends, and the runner starts on the first wake after that")
        return 3
    with open(d / "runner.out", "a") as out:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", str(d)], cwd=str(d),
                                stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    for _ in range(100):
        if runner_alive(d):
            print(f"runner started (pid {proc.pid})")
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    if runner_alive(d):     # another start won the race
        print("runner already running")
        return 0
    print(f"runner did not start; see {d / 'runner.out'}", file=sys.stderr)
    return 1


def clear(d: Path, config: str) -> int:
    f = d / "configs" / config
    if f.exists():
        f.unlink()
        print(f"cleared the drops of {config}")
    else:
        print(f"no drops recorded for {config}")
    return 0


def status(d: Path, job: str = "") -> int:
    if job:
        at = where_is(d, job)
        print(f"{job}: {at}")
        if at == "done":
            print((d / "done" / f"{job}.json").read_text().rstrip())
        elif at == "running":
            print((d / "running" / f"{job}.state.json").read_text().rstrip()
                  if (d / "running" / f"{job}.state.json").exists() else "")
        return 0
    state = (d / "state").read_text().strip() if (d / "state").exists() else "never ran"
    print(f"runner: {'alive' if runner_alive(d) else 'not running'}; {state}")
    for job_ in _running(d):
        print(f"running: {job_}")
    for f in sorted((d / "queue").glob("*.json")):
        print(f"queued:  {f.stem.split('-', 1)[1]}")
    for f in sorted((d / "configs").glob("*")):
        print(f"config {f.name}: {len(f.read_text().splitlines())} drop(s) in a row")
    if (d / "drops.log").exists():
        print("recent drops:\n" + "".join((d / "drops.log").read_text().splitlines(keepends=True)[-5:]).rstrip())
    return 0


class Runner:
    """The loop, holding runner.lock for its life."""

    def __init__(self, d: Path):
        self.d = d
        self.cfg = settings(_load(d / "config.json"))
        self._child = None
        self._helper = None     # the health or drop check under way: ended with the runner

    def stop(self, signum, frame) -> None:
        """TERM, INT or HUP: end the health or drop check under way (its own process group) and exit at
        once, so nothing of the runner outlives it. Jobs keep running: the next runner adopts them."""
        if self._helper is not None:
            _kill_group(self._helper.pid)
        self.log(f"runner stopped by signal {signum}")
        raise SystemExit(0)

    @contextlib.contextmanager
    def steady(self):
        """Hold off TERM, INT and HUP while the queue's files move, so a stop never leaves a job half moved
        or started without a record; a signal that came meanwhile acts on the way out."""
        sigs = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
        old = signal.pthread_sigmask(signal.SIG_BLOCK, sigs)
        try:
            yield
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old)

    def log(self, msg: str) -> None:
        with open(self.d / "runner.log", "a") as f:
            f.write(f"{_utc()} {msg}\n")

    def set_state(self, msg: str) -> None:
        _write(self.d / "state", f"{_utc()} {msg}\n")

    def sh(self, cmd: str, timeout: float, env: dict | None = None) -> tuple:
        """(exit code, last output line) of a shell command in its own group, killed at the timeout."""
        try:
            p = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=str(self.d), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 env={**os.environ, **(env or {})}, start_new_session=True)
        except OSError as e:
            return 127, str(e)
        self._helper = p
        try:
            out, _ = p.communicate(timeout=timeout or None)
        except subprocess.TimeoutExpired:
            _kill_group(p.pid)
            p.communicate()
            return 124, f"timed out after {timeout:.0f} s"
        finally:
            self._helper = None
        lines = [x for x in (out or "").splitlines() if x.strip()]
        return p.returncode, (lines[-1][:300] if lines else "")

    def healthy(self, need: int, job: str) -> str:
        """Wait until `need` health checks in a row pass: "" then, else why the gate gave up."""
        cmd = self.cfg["health"]
        if not cmd.strip():
            return ""
        t0, passes, last = _now(), 0, None
        while True:
            rc, out = self.sh(cmd, self.cfg["health_timeout_s"], {"TTP_DEVQ_JOB": job})
            if rc == 0:
                passes += 1
                if passes >= need:
                    self.set_state(f"{job}: health ok")
                    return ""
            else:
                passes = 0
                why = f"health check exit {rc}" + (f": {out}" if out else "")
                if why != last:
                    self.log(f"{job}: waiting: {why}")
                last = why
                self.set_state(f"{job}: waiting: {why}")
                if self.cfg["health_wait_s"] and _now() - t0 >= self.cfg["health_wait_s"]:
                    return f"the health gate refused for {self.cfg['health_wait_s']:.0f} s ({why})"
            time.sleep(self.cfg["health_poll_s"])

    def no_legacy(self, job: str) -> None:
        """Wait while an old per-task driver runs: it may start a device job of its own at any time."""
        told = False
        while True:
            legacy = legacy_drivers(self.d, self.cfg["legacy_driver"])
            if not legacy:
                return
            if not told:
                self.log(f"{job}: waiting: an old per-task driver still runs ({legacy[0]})")
                told = True
            self.set_state(f"{job}: waiting: an old per-task driver still runs ({legacy[0]})")
            time.sleep(self.cfg["health_poll_s"])

    def finish(self, job: str, st: dict, status: str, rc, reason: str) -> None:
        with self.steady():
            self._finish(job, st, status, rc, reason)

    def _finish(self, job: str, st: dict, status: str, rc, reason: str) -> None:
        spec = _load(self.d / "running" / f"{job}.json")
        marker = {"id": job, "status": status, "rc": rc, "log": st.get("log", ""), "reason": reason,
                  "task": spec.get("task", ""), "config": spec.get("config", job),
                  "attempts": st.get("attempts", 0), "drops": st.get("drops", []),
                  "started": st.get("started"), "finished": _now(), "finished_utc": _utc()}
        _write(self.d / "done" / f"{job}.json.t", json.dumps(marker, indent=1))
        (self.d / "running" / f"{job}.json").replace(self.d / "done" / f"{job}.spec.json")
        for leftover in [self.d / "running" / f"{job}.state.json", *(self.d / "running").glob(f"{job}.*.rc"),
                         *(self.d / "running").glob(f"{job}.*.lock")]:
            _rm(leftover)
        (self.d / "done" / f"{job}.json.t").replace(self.d / "done" / f"{job}.json")
        self.log(f"{job}: {status} rc={rc} {reason}")

    def save(self, job: str, st: dict) -> None:
        _write(self.d / "running" / f"{job}.state.json", json.dumps(st, indent=1))

    def launch(self, job: str, spec: dict, st: dict) -> None:
        with self.steady():
            self._launch(job, spec, st)

    def _launch(self, job: str, spec: dict, st: dict) -> None:
        """Start one attempt in a session of its own. It inherits a lock it holds while it lives, so a
        runner started after this one died can tell a job still running from one a reboot killed."""
        n = st.get("attempts", 0) + 1
        logf, rcf, lockf = (self.d / "logs" / f"{job}.{n}.log", self.d / "running" / f"{job}.{n}.rc",
                            self.d / "running" / f"{job}.{n}.lock")
        lock = open(lockf, "a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        wrapper = ('o="$1" r="$2" c="$3"; /bin/sh -c "$c" >>"$o" 2>&1 </dev/null; e=$?; '
                   'echo $e >"$r.tmp"; mv "$r.tmp" "$r"')
        try:
            p = subprocess.Popen(["/bin/sh", "-c", wrapper, "sh", str(logf), str(rcf), spec["cmd"]],
                                 cwd=spec.get("workdir") or str(Path.home()), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                 env={**os.environ, "TTP_DEVQ_JOB": job, "TTP_DEVQ_CONFIG": str(spec["config"]),
                                      "TTP_DEVQ_TIMEOUT_S": str(int(self.limit(spec)))},
                                 pass_fds=(lock.fileno(),))
        finally:
            lock.close()
        st.update(attempts=n, log=str(logf), cur={"n": n, "pid": p.pid, "rc": str(rcf), "lock": str(lockf),
                                                  "log": str(logf), "started": _now()})
        self.save(job, st)
        self.log(f"{job}: attempt {n} started (pid {p.pid})")
        self.set_state(f"{job}: running attempt {n}")
        self._child = p

    def limit(self, spec: dict) -> float:
        """A job's time limit in seconds (0 = none): its own timeout_s, else the runner's job_timeout_s,
        else the box's reservation cap. The job sees it as TTP_DEVQ_TIMEOUT_S, to size the timeouts of
        what it starts within it."""
        return job_limit(spec, self.cfg)

    def wait(self, job: str, spec: dict, st: dict) -> tuple:
        """(rc, how) of the current attempt: how is "" (it ended), "timeout" or "interrupted" (it is
        gone without an exit code: killed, or the host restarted)."""
        cur = st["cur"]
        rcf, lockf = Path(cur["rc"]), Path(cur["lock"])
        limit = self.limit(spec)
        child = self._child
        while True:
            if child is not None:
                child.poll()      # reap it, so its lock is released once it ends
            if rcf.exists():
                try:
                    return int(rcf.read_text().strip()), ""
                except ValueError:
                    return 1, ""
            if _free(lockf):
                if rcf.exists():
                    continue
                return None, "interrupted"
            if limit and _now() - cur["started"] >= limit:
                _kill_group(cur["pid"])
                return 124, "timeout"
            time.sleep(self.cfg["poll_s"])

    def drops_in_a_row(self, config: str) -> list:
        f = self.d / "configs" / config
        return f.read_text().splitlines() if f.exists() else []

    def record_drop(self, job: str, spec: dict, st: dict, rc, how: str, evidence: str) -> None:
        line = (f"utc={_utc()} job={job} task={spec.get('task', '')} config={spec['config']} attempt={st['cur']['n']} "
                f"rc={rc} kind={how or 'drop'} evidence=\"{evidence[:300]}\"")
        _append(self.d / "drops.log", line + "\n")
        _append(self.d / "configs" / spec["config"], f"{_utc()} {job} attempt {st['cur']['n']}\n")
        st.setdefault("drops", []).append(f"{_utc()} attempt {st['cur']['n']} rc={rc} {how or 'drop'}")
        self.log(f"{job}: DROP attempt {st['cur']['n']} rc={rc} {how or ''} {evidence[:200]}")

    def run_job(self, job: str) -> None:
        spec = _load(self.d / "running" / f"{job}.json")
        if not spec.get("cmd"):
            self.finish(job, {}, "failed", None, "the job spec is unreadable")
            return
        spec.setdefault("config", job)
        st = _load(self.d / "running" / f"{job}.state.json")
        st.setdefault("started", _now())
        need = 1
        if st.get("cur"):
            self.log(f"{job}: resuming after a runner restart")
        while True:
            if not st.get("cur"):
                too_long = over_cap(spec, self.cfg)
                if too_long:
                    self.finish(job, st, "failed", None, f"not started: {too_long}")
                    return
                drops = self.drops_in_a_row(spec["config"])
                if len(drops) >= int(self.cfg["max_drops"] or 0) > 0:
                    self.finish(job, st, "skipped", None,
                                f"config {spec['config']} dropped {len(drops)} times in a row; "
                                f"`ttp devq clear <runner> {spec['config']}` allows it again")
                    return
                self.no_legacy(job)
                why = self.healthy(need, job)
                if why:
                    self.finish(job, st, "skipped", None, why)
                    return
                try:
                    self.launch(job, spec, st)
                except OSError as e:
                    self.finish(job, st, "failed", None, f"could not start the job: {e}")
                    return
            rc, how = self.wait(job, spec, st)
            self._child = None
            evidence = ""
            if how == "interrupted":
                evidence = "the job is gone without an exit code (killed, or the host restarted)"
            elif rc != 0 and self.cfg["drop_check"].strip():
                drc, out = self.sh(self.cfg["drop_check"], self.cfg["health_timeout_s"],
                                   {"TTP_DEVQ_JOB": job, "TTP_DEVQ_RC": str(rc), "TTP_DEVQ_LOG": st["cur"]["log"],
                                    "TTP_DEVQ_STARTED": str(int(st["cur"]["started"])),
                                    "TTP_DEVQ_CONFIG": spec["config"]})
                evidence = out if drc == 0 else ""
                how = "drop" if drc == 0 else how
            if how in ("interrupted", "drop"):
                with self.steady():
                    self.record_drop(job, spec, st, rc, how, evidence)
                    st["cur"] = None
                    self.save(job, st)
                need = 2
                continue
            _rm(self.d / "configs" / spec["config"])
            if how == "timeout":
                self.finish(job, st, "failed", rc, f"timed out after {self.limit(spec):.0f} s")
            elif rc == 0:
                self.finish(job, st, "done", 0, "completed")
            else:
                self.finish(job, st, "failed", rc, f"exit {rc}")
            return

    def next_job(self) -> str:
        """The job to run now: one left running (its runner died), else the oldest queued."""
        running = _running(self.d)
        if running:
            return running[0]
        for q in sorted((self.d / "queue").glob("*.json")):
            job = q.stem.split("-", 1)[1]
            with self.steady():
                q.replace(self.d / "running" / f"{job}.json")
                self.log(f"{job}: dequeued")
            return job
        return ""

    def loop(self) -> int:
        _dirs(self.d)
        lock = open(self.d / "runner.lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, self.stop)
        _write(self.d / "runner.pid", f"{os.getpid()}\n")
        self.log(f"runner started (pid {os.getpid()})")
        idle_since = _now()
        while True:
            self.cfg = settings(_load(self.d / "config.json"))
            job = self.next_job()
            if job:
                self.run_job(job)
                idle_since = _now()
                continue
            self.set_state("idle")
            if _now() - idle_since >= self.cfg["idle_exit_s"]:
                self.log(f"idle {self.cfg['idle_exit_s']:.0f} s, exiting")
                return 0
            time.sleep(self.cfg["poll_s"])


def _kill_group(pid: int) -> None:
    for sig, pause in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 0)):
        try:
            os.killpg(pid, sig)
        except OSError:
            return
        end = _now() + pause
        while _now() < end:
            try:
                os.killpg(pid, 0)
            except OSError:
                return
            time.sleep(0.1)


def main(argv: list) -> int:
    """python3 devq.py <run|start|submit|probe|status|clear> <dir> [args]: the host side of `ttp devq`."""
    if len(argv) < 2:
        print(main.__doc__, file=sys.stderr)
        return 2
    op, d = argv[0], Path(os.path.expanduser(argv[1]))
    rest = argv[2:]
    if op == "run":
        return Runner(d).loop()
    if op == "start":
        return start(d, json.loads(rest[0]) if rest else {})
    if op == "submit":
        return submit(d, json.loads(rest[0]), json.loads(rest[1]))
    if op == "probe":
        return probe(d, rest[0])
    if op == "status":
        return status(d, rest[0] if rest else "")
    if op == "clear":
        return clear(d, rest[0])
    print(f"devq: unknown op {op}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
