"""Stop the detached jobs of a schedule that is turned off.

A command schedule's run starts with TTP_SCHEDULE=<name> in its environment (the daemon sets it), and so
does every process it starts, so a job the run leaves behind in a session of its own (a `ttp detach` job,
or a runner started with a new session) is known by that variable for as long as it runs. Once the
schedule is off, however it was turned off (schedule_set, the web app, a config or owner change, a freeze
that disables it), the daemon's schedule tick stops each such job through ttp's audited path: jobstop.end
for a `ttp detach` job (TTP_DETACH_RC), else SIGTERM then SIGKILL to its process groups, each signal in
the project's job-signal stream and one detached_job_stopped event. The tick sweeps at once after a
schedule is turned off, and at most every SWEEP_S while one stays off.

A launcher whose job must outlive the run (a paced benchmark runner) also records the job's group in
GROUPS_KEY (record), so `ttp detach --stop <schedule>` and the daemon find it even where its environment
cannot be read. A job started before the variable existed lacks both: it counts as the schedule's when its
session leader runs this project's `ttp` with the leading subcommand words of the schedule's command (read
through `ttp lock ... --` and `ttp detach ... --`), works under the project's root and belongs to no task. A job of a task (an llm schedule's run) follows its task, not the
schedule. Another project's processes are never matched (TTP_PROJECT)."""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import time
from pathlib import Path

ENV = "TTP_SCHEDULE"
REASON = "schedule.off"
BY = "schedule"
SWEEP_S = 60.0
GROUPS_KEY = "schedule_detached_groups"   # kv: pgid -> {schedule, start, what}: jobs a run left on purpose
TASK_VARS = ("TTP_TASK", "TTP_TASK_ID", "TTP_RUN_ID")
_WORD = re.compile(r"^[a-z][\w-]*$")
_SHELL = {"|", "||", "&&", ";", "&"}


def words(argv: list[str]) -> list[str]:
    """The leading subcommand words of the first `ttp` (or `python -m ttp`) in argv, read through
    `ttp lock <name> -- <command>`: ['model-bench', 'terminalbench'] for `ttp model-bench terminalbench
    --runner m`. [] when argv runs no ttp."""
    for i, a in enumerate(argv):
        if Path(a).name == "ttp" and not (i and argv[i - 1] == "-m") or a == "-m" and argv[i + 1:i + 2] == ["ttp"]:
            rest = argv[i + (2 if a == "-m" else 1):]
            out = []
            for w in rest:
                if not _WORD.match(w):
                    break
                out.append(w)
            if out[:1] in (["lock"], ["detach"]):   # wrappers: the command they run, else nothing to match
                return words(rest[rest.index("--") + 1:]) if "--" in rest else []
            return out
    return []


def command_words(cmd: str) -> list[str]:
    """words() of a schedule's shell command, up to its first shell operator."""
    try:
        toks = shlex.split(cmd or "")
    except ValueError:
        return []
    cut = next((i for i, t in enumerate(toks) if t in _SHELL), len(toks))
    return words(toks[:cut])


def _table() -> dict[int, tuple[int, int, int | None]]:
    from . import jobstop
    return jobstop._proc_table() or jobstop._ps_table()


def _sid(pid: int, row: tuple) -> int | None:
    if row[2] is not None:
        return row[2]
    try:
        return os.getsid(pid)
    except OSError:
        return None


def record(p, pid: int, what: str, schedule: str | None = None) -> None:
    """Session `pid` (its leader) was started to outlive the run that started it: kept in GROUPS_KEY with
    the schedule it belongs to (default: this process's ENV) until it is gone. Gone entries drop on write."""
    from .runner import proc_start
    with p.db.tx():
        groups = {k: v for k, v in (p.db.kv(GROUPS_KEY, {}) or {}).items()
                  if _alive(int(k), (v or {}).get("start"))}
        groups[str(pid)] = {"schedule": schedule if schedule is not None else os.environ.get(ENV),
                            "start": proc_start(pid), "what": what}
        p.db.set_kv(GROUPS_KEY, groups)


def recorded(p) -> dict[int, dict]:
    """The live entries of GROUPS_KEY (record), by pid."""
    return {int(k): v for k, v in (p.db.kv(GROUPS_KEY, {}) or {}).items()
            if isinstance(v, dict) and _alive(int(k), v.get("start"))}


def _link(pid: int, what: str) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/{what}")
    except OSError:
        return None


def _cgroup(pid: int | str) -> str | None:
    try:
        return Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None


def under_root(p, s: dict) -> bool:
    """Whether session `s`'s leader works inside the project's root (its code tree)."""
    cwd, root = s.get("cwd"), os.path.realpath(str(p.root))
    return bool(cwd) and (cwd == root or cwd.startswith(root.rstrip(os.sep) + os.sep))


def sessions(p, table: dict | None = None) -> list[dict]:
    """Each live session led by a process of this project with no task in its environment, or recorded in
    GROUPS_KEY: {sid, pids, pgids, env, words, argv, cwd, record}. The caller's own session is never one."""
    from . import jobstop
    table = _table() if table is None else table
    base = os.path.realpath(str(p.base))
    try:
        own = os.getsid(0)
    except OSError:
        own = None
    by_sid: dict[int, list[int]] = {}
    for pid, row in table.items():
        sid = _sid(pid, row)
        if sid:
            by_sid.setdefault(sid, []).append(pid)
    rec = recorded(p)
    out = []
    for sid, pids in by_sid.items():
        if sid == own or sid not in table or sid <= 1:
            continue
        env = jobstop._environ(sid) or {}
        mine = env.get("TTP_PROJECT") and os.path.realpath(env["TTP_PROJECT"]) == base
        if not (mine or sid in rec) or any(env.get(k) for k in TASK_VARS):
            continue
        argv = jobstop._argv(sid)
        out.append({"sid": sid, "pids": sorted(pids), "pgids": sorted({table[x][1] for x in pids}), "env": env,
                    "words": words(argv), "argv": argv, "cwd": _link(sid, "cwd"), "record": rec.get(sid)})
    return out


def jobs_of(p, name: str, payload: dict, live: list[dict]) -> list[dict]:
    """The sessions in `live` that are schedule `name`'s jobs (module doc), each with `how`."""
    legacy = command_words(str(payload.get("command") or ""))
    out = []
    for s in live:
        mark, rec = s["env"].get(ENV), s.get("record")
        if mark == name:
            out.append({**s, "how": f"{ENV}={name}"})
        elif rec and rec.get("schedule") == name:
            out.append({**s, "how": f"recorded {rec.get('what') or 'job'} of schedule {name}"})
        elif mark is None and not rec and legacy and s["words"][:len(legacy)] == legacy and under_root(p, s):
            out.append({**s, "how": f"runs `ttp {' '.join(legacy)}` with no task (started before {ENV})"})
    return out


def _alive(pid: int, start: str | None) -> bool:
    from . import jobstop
    from .runner import proc_start
    return jobstop._stat(pid) is not None and proc_start(pid) == start


def _end_session(p, name: str, s: dict, why: str, reason: str = REASON, of: str = "") -> dict | None:
    """SIGTERM then SIGKILL to the session's process groups (each signal recorded under `reason`), one
    event naming it as `of` (default: schedule `name`'s job)."""
    from . import detach, jobstop
    from .runner import proc_start
    starts = {pid: proc_start(pid) for pid in s["pids"]}
    tag = p.logs / f"schedule-{name}"   # absolute: a relative tag would land its .log in the caller's cwd
    sent = []
    for sig, wait in ((signal.SIGTERM, jobstop.PIDS_TERM_S), (signal.SIGKILL, jobstop.FREE_WAIT_S)):
        if not any(_alive(pid, t) for pid, t in starts.items()):
            break
        for g in s["pgids"]:
            try:
                os.killpg(g, sig)
            except OSError:
                continue
            sent.append((signal.Signals(sig).name, g))
            detach.signalled(tag, sig, g, reason, why, by=BY, state=p.state)
        deadline = time.monotonic() + wait
        while any(_alive(pid, t) for pid, t in starts.items()) and time.monotonic() < deadline:
            time.sleep(0.1)
    if not sent:
        return None
    left = [pid for pid, t in starts.items() if _alive(pid, t)]
    text = (f"stopped {of or f'detached job of schedule {name}'} (session {s['sid']}, {s['how']}): {why}; "
            + "; ".join(f"{sig} to group {g}" for sig, g in sent)
            + (f"; still running: {', '.join(map(str, left))}" if left else ""))
    out = {"schedule": name, "sid": s["sid"], "pids": s["pids"], "pgids": s["pgids"], "how": s["how"],
           "signals": sent, "left": left, "text": text}
    fp = f"schedule_job_stopped:{name}:{s['sid']}:{starts.get(s['sid'])}"
    if not p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status) VALUES(?,?,?,?,?,?,?,?)",
               (time.time(), "daemon", jobstop.EVENT, fp, "low", text, json.dumps(out), "handled"))
    return out


def stop(p, name: str, payload: dict, why: str, live: list[dict] | None = None) -> list[dict]:
    """Stop each running job of schedule `name` (jobs_of); the stops' summaries."""
    from . import jobstop
    out = []
    for s in jobs_of(p, name, payload, sessions(p) if live is None else live):
        rc = s["env"].get(jobstop.JOB_ENV)
        if rc and Path(rc).is_absolute() and jobstop.running(Path(rc)):
            r = jobstop.end(p, Path(rc), why, reason=REASON, fields={"schedule": name, "how": s["how"]})
        else:
            r = _end_session(p, name, s, why)
        if r:
            out.append(r)
    return out


def sweep(p) -> list[dict]:
    """stop() for every schedule that is off, from one read of the process table."""
    from .schedule import OFF
    off = p.db.q("SELECT name, payload FROM schedules WHERE enabled=0")
    if not off:
        return []
    live = sessions(p)
    out = []
    for row in off:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        out += stop(p, row["name"], payload if isinstance(payload, dict) else {}, OFF, live)
    return out
