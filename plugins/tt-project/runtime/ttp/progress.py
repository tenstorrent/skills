# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""What detached and looping drivers report: `ttp progress report <kind> --key <key> "<reason>"`.

A filler, recheck loop or guard that a worker or schedule started reports each counted result,
refusal, stall and failure here, one line from bash. Each report is one JSON line in the project's
state/progress.jsonl: {"ts", "key", "kind", "reason", "source"} plus the reporting driver's pid
(the caller's parent), boot id and, inside a `ttp detach` job, the job's .rc path, task and run. A refusal
may name its busy `resource` and `holder` (--holder, else holder_text from the lock state); describe names it.

The file is append-only under a lock (progress.lock) and synced per line, so concurrent reports
never interleave. It needs no daemon: a report made while the daemon is down is read once it is
back. It is bounded: past MAX_BYTES it rotates to progress.1.jsonl (one old generation is kept).

Readers: `streaks` is the model-free reading every consumer shares: per key, the reports since its
last result. N refusals, stalls or failures in a row with no result (waiting.progress.stall_after,
default 5) are a stalled key. The daemon raises one keyed observation per streak
(Daemon.watch_progress); `ttp status` and the web app list a driver whose process still runs as
stalled, not running (`stalled`).

Progress and stall check (`check`, the daemon's check_progress): model-free, each minute, so the
coordinator hears about work that stopped producing results before the user does. It reads the
reports above through `entries` (both generations, so a rotation loses no line) and leaves report
streaks to `streaks`/Daemon.watch_progress: one detector, one wake per stalled driver key.

Progress clock: the time since the last counted result (a task done, a worker hand-off, a driver's
`result` report for its key, or made inside a task's run), kept per open task, per resource open
tasks name and for the project. It judges only work that has run: past its threshold while worker
runs are running, one keyed `no_progress` event names what is stuck, since when and the last reason:

- task: a task with a worker run running that has had no result for `task_h` (2) hours since the
  later of that run's start and its last counted result;
- resource: a resource held by running worker runs with no result on any task naming it for
  `resource_h` (2) hours since the earliest of those runs started;
- project: no result anywhere for `project_h` (2) hours since the earliest running worker run started.

A queued task, never started or queued again after a run, is never judged here: whether dispatch
would start it now is Daemon.check_stall's (its _start_hold model), not a second clock here, and
no key here names one. Why dispatch holds a ready task (a pause, a busy slot, a lock and its holder)
is dispatch's record (Daemon._resource_hold), which check_stall's own stall message names; this
module works out no lock, pause or alias. Known gap: check_stall wakes only when no worker run at
all has started for coordinator.stall_wake_h, so one ready task that dispatch passes over while
other runs keep starting is reported by neither.

Machine changes: `change-probe:<id>`, one `change_probe` event once an open machine change of this
project with an until-probe has had it pass for `change_probe_m` (15) minutes while the task it
names is still queued or waiting. It clears at once when the change closes or the task wakes.

Repeat detectors, each one keyed `stall` event with the count, first and last seen and the reason:

- probe: a waiting task's retry_when or a deferred task's start_when still says not yet after
  `probe_n` (10) verdicts and `probe_h` (1) hours (waits on the task's own job, a push, CI, another
  task landing or a time are expected to be long and left out, as is a wait on another project's
  reply (`ttp ack-wait` or any probe ackwait.is_probe accepts, such as the project's own ack_wait.py
  wrapper: the daemon's wait_stale covers it), and so is a user pause,
  userpause.is_probe: only the user's word ends it). It is flagged "waits on what it
  fixes" when the probe waits on a resource that is down or paused and the task is that resource's
  fix, or a task holding the resource waits on this one. Only the answers since the task's
  start_after last changed count; a task whose start_after is still ahead (a planned deferral) never
  raises it, and its count (with a stall raised on it) is dropped, as is a start_when's whose probe
  has not answered for QUIET_S (it is not running, so it cannot be judged);
- runs: a task's (or one schedule's) worker runs failing `fail_n` (3) times in a row the same way;
- schedule: a schedule failing `repeat_n` (5) times in a row with the same status. A skip on its own
  daily cap is not counted here: the daemon's schedule-budget observation (Daemon._schedule_skip)
  reports it, once a day.

Each key wakes once per condition. It wakes again only when its count (or stuck time) reaches
`escalate_x` (2) times what it was at the last wake, or after `rewake_h` (12) hours. A probe stall's
count grows with time alone (one verdict per probe interval), so it never escalates: one wake per
wait, then one per `rewake_h`. Hysteresis: a
raised key that drops out of a check is not cleared at once (a resource or project key drops out
between two runs). It stays raised, quiet, and clears only once it has stayed healthy for
`clear_n` (3) checks in a row spanning at least `clear_min` (30) minutes; coming back before that
continues the same raise and never wakes as new. Schedule repeats also lapse after `quiet_h` (6)
hours with no repeat. Each cleared key is kept for the daily review with how long it took. A mute
(source `daemon:progress`, match the key) keeps a key quiet. Thresholds live under
`coordinator.progress_check` in project.json (0 turns one off).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import ackwait, startgate, userpause
from .db import DB, TERMINAL_TASK_STATES, deferral, load_result
from .project import durable_append

LOG, OLD, LOCK = "progress.jsonl", "progress.1.jsonl", "progress.lock"
MAX_BYTES = 256 * 1024
REASON_MAX = 500
KINDS = ("result", "refusal", "stall", "failure")
BAD = frozenset(KINDS) - {"result"}   # count toward a stall; a result ends one
JOB_ENV = "TTP_DETACH_RC"   # set by `ttp detach` in its job's environment: the job's .rc path
DEFAULTS = {"stall_after": 5, "window_s": 86400}


def settings(cfg: dict | None) -> dict:
    got = ((cfg or {}).get("waiting") or {}).get("progress") or {}
    return {**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS and v is not None}}


def _one_line(text: str, cap: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:cap]


def record(kind: str, key: str, reason: str, env: dict | None = None, now: float | None = None,
           resource: str = "", holder: str = "") -> dict:
    """The line a report writes, from the caller's environment; a refusal's resource and its holder
    (holder_text) only when given."""
    from .runner import boot_id
    env = os.environ if env is None else env
    job = env.get(JOB_ENV) or None
    task, run = env.get("TTP_TASK") or None, env.get("TTP_RUN_ID") or None
    pid = os.getppid()
    who = [f"task {task}" if task else "", f"run {run}" if run else "",
           f"job {Path(job).stem}" if job else "", f"pid {pid}"]
    return {"ts": round(time.time() if now is None else now, 3), "key": _one_line(key, 200), "kind": kind,
            "reason": _one_line(reason, REASON_MAX), "source": " ".join(w for w in who if w),
            "pid": pid, "boot": boot_id(), "job": job, "task": task, "run": run,
            **({"resource": _one_line(resource, 200)} if (resource or "").strip() else {}),
            **({"holder": _one_line(holder, REASON_MAX)} if (holder or "").strip() else {})}


def holder_text(p, resource: str) -> str:
    """Who holds `resource` now, as `ttp lock --probe --json` reads it (lockprobe.answer): each holder's
    label (a task's "task #N (run R)", else "external <label>"), pid and live or stale; "nobody (free)"
    or "nobody (paused: <why>)"; "unknown" when the lock state cannot be read. Never raises."""
    from . import hold, lockprobe
    try:
        _, said = lockprobe.answer(p, p.config(), resource, as_json=True)
        if said.get("paused"):
            return f"nobody (paused: {said.get('reason') or '?'})"
        out = []
        for h in said.get("holders") or []:
            lbl = str(h.get("holder") or "?")
            state = ("stale" if h.get("stale") or h.get("alive") is False else
                     "live" if h.get("alive") else "liveness unknown")
            pid = f", pid {h['pid']}" if h.get("pid") else ""
            out.append(f"{lbl if hold.parse(lbl) else f'external {lbl}'}{pid} ({state})")
        return "; ".join(out) or ("nobody (free)" if said.get("free") else "unknown")
    except Exception:
        return "unknown"


def append(state: Path, rec: dict, max_bytes: int = MAX_BYTES) -> None:
    """Append one report under the lock, rotating first when it would pass max_bytes. Raises OSError
    when the state folder cannot be written (the caller says so and still exits 0)."""
    state = Path(state)
    line = json.dumps(rec, sort_keys=True) + "\n"
    state.mkdir(parents=True, exist_ok=True)
    with open(state / LOCK, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        log = state / LOG
        try:
            size = log.stat().st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(line) > max_bytes:
            os.replace(log, state / OLD)
        durable_append(log, line)


def entries(state: Path) -> list[dict]:
    """Every report kept, oldest first; unreadable lines are skipped."""
    out = []
    for name in (OLD, LOG):
        try:
            text = (Path(state) / name).read_text(errors="replace")
        except OSError:
            continue
        for ln in text.splitlines():
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("key") and rec.get("kind") in KINDS:
                out.append(rec)
    return out


def streaks(recs: list[dict]) -> dict[str, dict]:
    """Per key, its reports since its last result: {"bad": how many, "first"/"last": the streak's
    first and last report (None while bad is 0), "kinds": count per kind, "result": the last result}."""
    out: dict[str, dict] = {}
    for r in sorted(recs, key=lambda x: float(x.get("ts") or 0)):
        s = out.setdefault(r["key"], {"key": r["key"], "bad": 0, "first": None, "last": None, "kinds": {},
                                      "result": None})
        if r["kind"] == "result":
            s.update(bad=0, first=None, last=None, kinds={}, result=r)
            continue
        s["bad"] += 1
        s["first"] = s["first"] or r
        s["last"] = r
        s["kinds"][r["kind"]] = s["kinds"].get(r["kind"], 0) + 1
    return out


def stalled_keys(recs: list[dict], n: int, window_s: float, now: float) -> list[dict]:
    """The keys whose last n or more reports are refusals, stalls or failures with no result, the
    latest of them within window_s."""
    return [s for s in streaks(recs).values()
            if s["bad"] >= max(1, int(n)) and now - float(s["last"].get("ts") or 0) <= window_s]


def _alive(r: dict) -> bool:
    """Whether the driver that made report r still runs: its detached job (detach.state), else its pid
    on this boot."""
    from . import detach
    from .runner import same_boot
    if r.get("job"):
        try:
            return detach.state(Path(r["job"]))[0] == "running"
        except OSError:
            return False
    try:
        pid = int(r.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 1 or not same_boot(r.get("boot")):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def describe(s: dict) -> str:
    """'4 refusals, 1 failure, no result since <first report>: <last reason>' without the time."""
    kinds = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in sorted(s["kinds"].items(), key=lambda kv: (-kv[1], kv[0])))
    held = f" ({s['last'].get('resource') or 'resource'} held by {h})" if (h := s["last"].get("holder")) else ""
    return f"{kinds} in a row, no result: {s['last'].get('reason') or '(no reason given)'}{held}"


def stalled(state: Path, cfg: dict | None, now: float | None = None) -> list[dict]:
    """Drivers whose process still runs but whose last stall_after reports are refusals, stalls or
    failures with no result: {"key", "source", "bad", "since", "line"}, the longest streak first."""
    s = settings(cfg)
    now = time.time() if now is None else now
    out = []
    for k in stalled_keys(entries(state), s["stall_after"], float(s["window_s"]), now):
        if not _alive(k["last"]):
            continue
        out.append({"key": k["key"], "source": k["last"].get("source") or "", "bad": k["bad"],
                    "since": float(k["first"].get("ts") or 0), "line": describe(k)})
    return sorted(out, key=lambda x: -x["bad"])


# ---- progress and stall check (the daemon's check_progress) ----

KEY = "progress_check"          # coordinator.progress_check: thresholds
STATE_KEY = "progress_state"    # kv: results seen, counters, raised keys, cleared history
SOURCE = "daemon:progress"      # the events' source, for observation mutes
# Each key kind's event kind: no_progress, stall and change-probe (event change_probe).
EVENTS = {"no_progress": "no_progress", "stall": "stall", "change-probe": "change_probe"}
EVENT_KINDS = ("no_progress", "stall", "change_probe")
CHECK_KINDS = ("no_progress", "stall", "change-probe")
HEADS = {"no_progress": "No progress: running work produced no result past its threshold.",
         "stall": "Stall: the same not-yet or failure keeps repeating.",
         "change_probe": "Machine change due its undo: its until-probe passed, the task that owns it still waits."}
# clear_n/clear_min: hysteresis. A raised key clears only after clear_n healthy checks in a row that
# span clear_min minutes: at the daemon's 60 s check that is 3 checks and half an hour, long enough
# for slots that fill and free, short enough that a fixed key leaves the digest within the hour.
CHECK_DEFAULTS = {"task_h": 2, "resource_h": 2, "project_h": 2, "probe_n": 10, "probe_h": 1, "repeat_n": 5,
                  "fail_n": 3, "escalate_x": 2, "rewake_h": 12, "quiet_h": 6, "clear_n": 3, "clear_min": 30,
                  "change_probe_m": 15}
# A start_when probe starts at most every startgate.MAX_EVERY_S (600 s; a healthy long wait backs off to
# it) and answers within daemon.PROBE_TIMEOUT_S (60 s): no answer for two intervals and a timeout means
# it is not running. A shorter span dropped the count of a backed-off or briefly slow probe and re-raised
# its stall as new.
QUIET_S = 2 * startgate.MAX_EVERY_S + 60
HEADER = "## Progress (stuck work: re-plan, re-tier, re-point the probe, route around, cancel or fix the cause)"
BAD_RUNS = ("failed", "timeout", "stalled")
# Waits expected to be long, whose producer has its own clock: the task's own detached job or checks,
# a push, CI, another task landing, another project's reply, an upstream fix, a time.
OWN_WAIT_RE = re.compile(r"detach --check|checks --result|push --result|\bttp ci\b|ack-wait|landed:|ttp landed|"
                         r"upstream --fixed|^\s*test \"\$\(date")
# A wait only the user ends: the hand-off's wait_kind (unblock.USER_WAIT_KINDS), or a waiting_for or
# blocked_reason naming the user on a task that owns an open obligation.
NAMES_USER_RE = re.compile(r"\buser(?:'s|’s)?\b(?!-(?:facing|space|land|agent))", re.I)
FIX_RE = re.compile(r"\b(fix\w*|repair\w*|recover\w*|restor\w*|reboot\w*|reset\w*|heal\w*|unstick\w*|revive\w*)\b", re.I)
REASON_CHARS = 200
HISTORY_KEEP = 200
RESULTS_KEEP = 500
DAY = 86400


def check_settings(cfg: dict | None) -> dict:
    got = ((cfg or {}).get("coordinator") or {}).get(KEY) or {}
    out = dict(CHECK_DEFAULTS)
    if isinstance(got, dict):
        for k in CHECK_DEFAULTS:
            try:
                out[k] = max(0.0, float(got.get(k, CHECK_DEFAULTS[k]) or 0))
            except (TypeError, ValueError):
                pass
    return out


def _norm(text: str) -> str:
    """A reason with its numbers and hex ids masked, so 'busy for 61 s' repeats 'busy for 600 s'."""
    return re.sub(r"\d+", "#", re.sub(r"\b[0-9a-f]{7,40}\b", "#", " ".join(str(text or "").split()).lower()))


def _clip(text: Any, n: int = REASON_CHARS) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _dur(s: float) -> str:
    s = max(0.0, s)
    return f"{s / 3600:.1f} h" if s >= 5400 else f"{int(s // 60)} min"


def _ts(rec: dict, default: float) -> float:
    try:
        return float(rec.get("ts"))
    except (TypeError, ValueError):
        return default


def load(db: DB) -> dict:
    st = db.kv(STATE_KEY) or {}
    if not isinstance(st, dict):
        st = {}
    st.pop("ready", None)   # an older build's ready clocks: never-started work is check_stall's
    for k in ("results", "probes", "scheds", "raised"):
        st.setdefault(k, {})
    st.setdefault("history", [])
    return st


def _save(db: DB, st: dict) -> None:
    if len(st["results"]) > RESULTS_KEEP:
        st["results"] = dict(sorted(st["results"].items(), key=lambda kv: kv[1])[-RESULTS_KEEP:])
    st["history"] = st["history"][-HISTORY_KEEP:]
    db.set_kv(STATE_KEY, st)


def _take_results(st: dict, recs: list[dict], now: float) -> None:
    """Drivers' `result` reports as counted results: per report key, and per task for one made inside
    a task's run (`task:<id>`). Kept in the state, so a result rotated out of the file still counts."""
    for rec in recs:
        if rec["kind"] != "result":
            continue
        ts = _ts(rec, now)
        for k in {str(rec["key"]).strip()[:200], f"task:{rec['task']}" if rec.get("task") else ""} - {""}:
            st["results"][k] = max(ts, float(st["results"].get(k) or 0))


# ---- inputs the daemon feeds ----

def _after(t: dict | None) -> float | None:
    return deferral(t).get("after") if t else None


def note_probe(db: DB, tid: int, probe: str, rc: Any, reason: str, now: float | None = None) -> None:
    """A probe (retry_when or start_when) said not yet: count it for its task. A count made under
    another start_after starts over: only the answers since it last changed count."""
    now = time.time() if now is None else now
    st = load(db)
    rec = st["probes"].get(str(tid))
    after = _after(db.task(tid))
    if rec and rec.get("probe") == probe and rec.get("after") == after and now <= float(rec["last"]):
        return   # this verdict is counted already
    if rec and rec.get("probe") == probe and rec.get("after") == after:
        rec.update(n=int(rec["n"]) + 1, last=now, rc=rc, reason=_clip(reason) or rec.get("reason"))
    else:
        st["probes"][str(tid)] = {"probe": probe, "n": 1, "first": now, "last": now, "rc": rc,
                                  "reason": _clip(reason), "after": after}
    _save(db, st)


def drop_probe(db: DB, tid: int, now: float | None = None) -> None:
    """The task's start_after moved into the future (task_update): its probe stops until then, so
    its not-yet count, a stall raised on it and a deferral_probe_broken finding not yet handled are
    dropped. Counting starts afresh once the probe runs again; a broken probe is raised again then."""
    now = time.time() if now is None else now
    st = load(db)
    st["probes"].pop(str(tid), None)
    _unraise(st, f"stall:probe:#{tid}", now)
    _save(db, st)
    db.x("UPDATE events SET status='handled' WHERE status='queued' AND kind='deferral_probe_broken' AND task=?",
         (tid,))


def forget_probe(db: DB, tid: int) -> None:
    """The task's probe passed: progress for its wait."""
    st = load(db)
    if st["probes"].pop(str(tid), None) is not None:
        _save(db, st)


def _schedule_failed(status: str) -> bool:
    s = str(status or "")
    return s == "no command" or s.startswith("error")


def note_schedule(db: DB, name: str, status: str, now: float | None = None) -> None:
    """A schedule's trigger ended with `status`. Failures count when they repeat; a run that did
    something ends the stint. Skips change nothing: the expected ones (the budget gate, an open run, a
    debounce, the idle guard, a precheck), and one on its own daily cap, which the daemon's
    schedule-budget observation reports (Daemon._schedule_skip), so it is never reported twice."""
    from . import schedule as sched
    now = time.time() if now is None else now
    if sched.skip(status):
        return
    st = load(db)
    rec = st["scheds"].get(name)
    if _schedule_failed(status):
        if rec and rec.get("norm") == _norm(status):
            rec.update(n=int(rec["n"]) + 1, last=now, reason=_clip(status))
        else:
            st["scheds"][name] = {"n": 1, "first": now, "last": now, "reason": _clip(status), "norm": _norm(status)}
    elif rec is None:
        return
    else:
        st["scheds"].pop(name, None)
    _save(db, st)


# ---- conditions ----

def _resources(task: dict) -> set[str]:
    from .coordinator import task_resources
    return task_resources(task)


def _running(db: DB) -> list[dict]:
    """Running worker runs with their task: the only work the no_progress keys judge."""
    return db.q("SELECT r.id, r.started, r.task, t.labels, t.title, t.blocked_reason, t.status tstatus "
                "FROM runs r LEFT JOIN tasks t ON t.id=r.task WHERE r.role!='coordinator' AND r.status='running'")


def _last_results(db: DB, st: dict, now: float) -> dict[int, float]:
    """Task id -> its last counted result (a worker hand-off, done, a driver's result for `task:<id>`)."""
    out: dict[int, float] = {}
    for r in db.q("SELECT task, MAX(ended) t FROM runs WHERE role!='coordinator' AND status='ok' "
                  "AND task IS NOT NULL AND ended>? GROUP BY task", (now - 30 * DAY,)):
        out[int(r["task"])] = float(r["t"] or 0)
    for r in db.q("SELECT id, updated FROM tasks WHERE status='done' AND updated>?", (now - 30 * DAY,)):
        out[int(r["id"])] = max(out.get(int(r["id"]), 0.0), float(r["updated"] or 0))
    for k, ts in st["results"].items():
        m = re.fullmatch(r"task:#?(\d+)", k)
        if m:
            out[int(m.group(1))] = max(out.get(int(m.group(1)), 0.0), float(ts))
    return out


def _task_reason(db: DB, run: dict) -> str:
    if run.get("blocked_reason"):
        return _clip(run["blocked_reason"])
    r = db.one("SELECT status, exit_code FROM runs WHERE task=? AND role!='coordinator' AND id<? "
               "ORDER BY id DESC LIMIT 1", (run["task"], run["id"]))
    return "run running, no result yet" + (
        f"; previous run {r['status']}" + (f" (exit {r['exit_code']})" if r["exit_code"] not in (None, 0) else "")
        if r else "")


def no_progress(db: DB, cfg: dict, s: dict, st: dict, now: float, open_streaks: dict | None = None) -> list[dict]:
    """Running work with no counted result past its threshold. Only work that has run is judged: a
    queued task that never started (or waits to start again) is Daemon.check_stall's. `open_streaks`:
    drivers' report streaks (`streaks`), whose last refusal or failure names why a resource is stuck."""
    running = _running(db)
    if not running:
        return []
    paused = db.paused_resources()
    last = _last_results(db, st, now)
    out = []
    if s["task_h"]:
        latest: dict[int, dict] = {}
        for run in running:
            if run["task"] is not None and run["tstatus"] not in TERMINAL_TASK_STATES and \
                    float(run["started"] or 0) >= float((latest.get(int(run["task"])) or {}).get("started") or 0):
                latest[int(run["task"])] = run
        for tid, run in sorted(latest.items()):
            since = max(float(run["started"] or now), last.get(tid, 0.0))
            if now - since >= s["task_h"] * 3600:
                out.append({"kind": "no_progress", "key": f"task:#{tid}", "task": tid,
                            "what": f"task #{tid} {_clip(run['title'], 60)}", "since": since, "n": now - since,
                            "reason": _task_reason(db, run)})
    trouble = db.kv("resource_trouble") or {}
    if s["resource_h"]:
        held: dict[str, list[float]] = {}
        for run in running:
            for r in _resources({"labels": run.get("labels")}):
                held.setdefault(r, []).append(float(run["started"] or now))
        for r, starts in sorted(held.items()):
            ids = [int(x["id"]) for x in db.q("SELECT id, labels FROM tasks WHERE labels LIKE ?", (f'%:{r}"%',))
                   if r in _resources(x)]
            done = max([last.get(i, 0.0) for i in ids] + [float(st["results"].get(f"resource:{r}") or 0),
                                                          float(st["results"].get(r) or 0)])
            since = max(done, min(starts))
            if now - since < s["resource_h"] * 3600:
                continue
            why = (f"paused: {paused[r].get('reason') or '?'}" if r in paused else
                   "resource trouble: its tasks keep failing" if r in trouble else
                   next((f"{x['last']['kind']}: {x['last'].get('reason') or '?'}"
                         for k, x in (open_streaks or {}).items() if x["bad"] and r in k), "")
                   or "work on it is running, no result")
            out.append({"kind": "no_progress", "key": f"resource:{r}", "what": f"resource {r}", "since": since,
                        "n": now - since, "reason": _clip(why)})
    if s["project_h"]:
        done = max([0.0, *last.values(), *(float(v) for v in st["results"].values())])
        since = max(done, min(float(r["started"] or now) for r in running))
        if now - since >= s["project_h"] * 3600:
            out.append({"kind": "no_progress", "key": "project", "what": "the project", "since": since,
                        "n": now - since, "reason": f"{len(running)} worker run(s) running, no result"})
    return out


def _self_ref(db: DB, t: dict, probe: str, rc: Any, paused: dict, trouble: dict) -> str:
    """'waits on what it fixes: <resource> ...' when the probe waits on a resource that is down or
    paused and the task is its fix (or a task holding it waits on this one); else ''."""
    from . import locks, netwait
    named = set(locks.probe_resources(probe))
    hosts = set(netwait.hosts(probe)) if rc == 255 else set()
    down = {r: f"paused ({paused[r].get('reason') or 'no reason'})" for r in named | hosts if r in paused}
    down.update({r: "in resource trouble" for r in named | hosts if r in trouble and r not in down})
    down.update({h: "unreachable" for h in hosts if h not in down})
    try:
        labels = json.loads(t.get("labels") or "[]")
    except ValueError:
        labels = []
    title, mine = str(t.get("title") or ""), _resources(t)
    for r, how in sorted(down.items()):
        fix = any(isinstance(lb, str) and lb.startswith(f"outage:{r}:") for lb in labels) or \
            ((r in mine or r.lower() in title.lower()) and FIX_RE.search(title))
        if fix:
            return f"waits on what it fixes: {r} is {how} and this task is its fix"
        for h in db.q("SELECT id, depends_on, labels, result, status FROM tasks WHERE id!=? AND status NOT IN "
                      f"({','.join('?' * len(TERMINAL_TASK_STATES))}) AND labels LIKE ?",
                      (t["id"], *TERMINAL_TASK_STATES, f'%:{r}"%')):
            if r not in _resources(h):
                continue
            hp = str(load_result(h["result"]).get("retry_when") or "") + " " + str(deferral(h).get("when") or "")
            try:
                deps = [int(x) for x in json.loads(h["depends_on"] or "[]")]
            except (ValueError, TypeError):
                deps = []
            if t["id"] in deps or re.search(rf"(#|--task\s+){t['id']}\b", hp):
                return f"waits on what it fixes: {r} is {how} and its holder #{h['id']} waits on this task"
    return ""


def _user_wait(db: DB, t: dict, res: dict) -> bool:
    """Whether task `t` waits model-free on the user's step: its hand-off's wait_kind is `user` (or
    `user-action`), or it owns an open obligation and its waiting_for or blocked_reason names the user."""
    from .unblock import USER_WAIT_KINDS
    if str(res.get("wait_kind") or "").strip().lower() in USER_WAIT_KINDS:
        return True
    said = " ".join(str(x or "") for x in (res.get("waiting_for"), t.get("blocked_reason")))
    if not NAMES_USER_RE.search(said):
        return False
    from .obligations import _load, _open_owners
    return str(t["id"]) in _open_owners(_load(db))


def probe_stalls(db: DB, s: dict, st: dict, now: float, current_probe: Callable[[dict], str | None],
                 cfg: dict | None = None, gone: set | None = None, harness=None) -> list[dict]:
    """Probe stalls; adds to `gone` the raised keys whose count was dropped as no longer valid. `harness`:
    the project's harness dir, so its own ack-wait wrapper counts as a reply wait (ackwait.is_probe)."""
    out = []
    paused, trouble = db.paused_resources(), db.kv("resource_trouble") or {}
    for k, rec in list(st["probes"].items()):
        t = db.task(int(k))
        if not t or t["status"] in TERMINAL_TASK_STATES or t["status"] == "running" or current_probe(t) != rec["probe"]:
            st["probes"].pop(k)   # the wait ended, or its probe was re-pointed
            continue
        d = deferral(t)
        after = d.get("after")
        if (after or 0) > now or rec.get("after") != after \
                or (d.get("when") == rec["probe"] and now - float(rec["last"]) > QUIET_S):
            # A planned deferral, answers from before its start_after last changed, or a start_when
            # probe that is not running: nothing to judge, and a stall raised on it ends now.
            st["probes"].pop(k)
            if gone is not None:
                gone.add(f"stall:probe:#{k}")
            continue
        res = load_result(t["result"])
        if t["status"] == "waiting" and res.get("wait_kind") == "self":
            continue   # its own job or window: the wait checks cover a job that hangs
        if _user_wait(db, t, res):
            continue   # only the user's step ends it: no worker can speed it up (the probe keeps counting)
        if userpause.is_probe(rec["probe"], cfg):
            continue   # a user pause: only the user's word ends it, so saying not yet is no stall
        if not s["probe_n"] or OWN_WAIT_RE.search(rec["probe"]) or ackwait.is_probe(rec["probe"], cfg, harness) \
                or int(rec["n"]) < s["probe_n"] \
                or now - float(rec["first"]) < s["probe_h"] * 3600:
            continue
        out.append({"kind": "stall", "key": f"probe:#{t['id']}", "task": t["id"],
                    "what": f"task #{t['id']} {_clip(t['title'], 60)}: its probe said not yet {rec['n']} times",
                    "since": float(rec["first"]), "last": float(rec["last"]), "n": int(rec["n"]),
                    "reason": f"{rec.get('reason') or 'not yet'}; probe: {_clip(rec['probe'], 120)}",
                    "selfref": _self_ref(db, t, rec["probe"], rec.get("rc"), paused, trouble)})
    return out


def run_stalls(db: DB, s: dict, now: float) -> list[dict]:
    """A task's, or one schedule's, latest worker runs that failed `fail_n` times in a row the same way."""
    if not s["fail_n"]:
        return []
    groups: dict[str, list[dict]] = {}
    for r in db.q("SELECT r.task, r.status, r.exit_code, r.note, r.ended, t.origin, t.labels, t.status tstatus, "
                  "t.title FROM runs r JOIN tasks t ON t.id=r.task WHERE r.role!='coordinator' AND r.ended>? "
                  "AND r.status NOT IN ('running','lost') ORDER BY r.ended DESC, r.id DESC", (now - DAY,)):
        if r["origin"] == "schedule":
            labels = json.loads(r["labels"] or "[]")
            key = f"schedule-runs:{labels[0]}" if labels else f"runs:#{r['task']}"
        else:
            key = f"runs:#{r['task']}"
        groups.setdefault(key, []).append(r)
    out = []
    for key, rows in groups.items():
        if key.startswith("runs:") and rows[0]["tstatus"] in TERMINAL_TASK_STATES:
            continue
        sig, n = None, 0
        for r in rows:
            try:
                note = json.loads(r["note"] or "{}")
            except ValueError:
                note = {}
            why = _clip((note.get("error") if isinstance(note, dict) else "") or
                        f"{r['status']}" + (f", exit {r['exit_code']}" if r["exit_code"] is not None else ""))
            if r["status"] not in BAD_RUNS or (isinstance(note, dict) and note.get("lost_to_reboot")):
                break
            mine = (r["status"], r["exit_code"], _norm(why))
            if sig is not None and mine != sig:
                break
            sig, n, reason, first = mine, n + 1, why, float(r["ended"] or now)
        if n >= s["fail_n"]:
            what = (f"schedule {key.split(':', 1)[1]}" if key.startswith("schedule-runs:")
                    else f"task #{rows[0]['task']} {_clip(rows[0]['title'], 60)}")
            out.append({"kind": "stall", "key": key, "what": f"{what}: {n} runs in a row failed the same way",
                        "since": first, "last": float(rows[0]["ended"] or now), "n": n, "reason": reason,
                        **({"task": rows[0]["task"]} if key.startswith("runs:") else {})})
    return out


def schedule_stalls(db: DB, s: dict, st: dict, now: float) -> list[dict]:
    out = []
    names = {r["name"] for r in db.q("SELECT name FROM schedules WHERE enabled=1")}
    for name, rec in list(st["scheds"].items()):
        if name not in names or (s["quiet_h"] and now - float(rec["last"]) >= s["quiet_h"] * 3600):
            st["scheds"].pop(name)
            continue
        if s["repeat_n"] and int(rec["n"]) >= s["repeat_n"]:
            out.append({"kind": "stall", "key": f"schedule:{name}",
                        "what": f"schedule {name}: the same status {rec['n']} times in a row",
                        "since": float(rec["first"]), "last": float(rec["last"]), "n": int(rec["n"]),
                        "reason": rec["reason"]})
    return out


# ---- waking ----

def _line(c: dict, now: float, where: Any = None) -> str:
    when = _since(c["since"], now, where)
    text = f"{c['what']}, no progress since {when} ({_dur(now - c['since'])})"
    if c.get("last") and c["kind"] == "stall":
        text += f", last {_since(c['last'], now, where)}"
    text += f"; why: {c['reason'] or '?'}"
    if c.get("selfref"):
        text += f"; {c['selfref']}"
    return f"[{_key(c)}] {text}"


def _key(c: dict) -> str:
    """The key an event shows: bare for its own kind's (task:#3), with the kind for another (change-probe:4)."""
    return c["key"] if EVENTS.get(c["kind"]) == c["kind"] else f"{c['kind']}:{c['key']}"


def _since(ts: float, now: float, where: Any) -> str:
    if where is not None:
        from . import timefmt
        try:
            return timefmt.short(ts, where, now)
        except Exception:
            pass
    return f"{_dur(now - ts)} ago"


def _decide(st: dict, conds: list[dict], s: dict, now: float, kinds: tuple = CHECK_KINDS,
            gone: set | frozenset = frozenset()) -> list[tuple[dict, str]]:
    """(condition, why it wakes: new, escalated or still) for each key due a wake; updates `raised`.
    A raised key missing from this check counts one healthy check (ok_n, since ok_since) and moves to
    `history` only after clear_n of them spanning clear_min minutes (hysteresis); seen again before
    that, it continues its raise: no new wake, and escalation still measured from its last wake.
    Only raised keys of `kinds` (the caller's detectors) are judged; one in `gone` (its condition
    really ended: the task started, the change closed) clears at once."""
    due, seen = [], set()
    for c in conds:
        key = f"{c['kind']}:{c['key']}"
        seen.add(key)
        r = st["raised"].get(key)
        why = ("new" if r is None else
               "escalated" if s["escalate_x"] and not _by_time(c) and float(c["n"]) >= float(r["n"]) * s["escalate_x"] else
               "still" if s["rewake_h"] and now - float(r["at"]) >= s["rewake_h"] * 3600 else "")
        keep = {"kind": c["kind"], "what": c["what"], "since": c["since"], "reason": c["reason"],
                "cur": c["n"], "selfref": c.get("selfref") or "", "first": (r or {}).get("first", now),
                "ok_n": 0, "ok_since": None}
        st["raised"][key] = {**(r or {}), **keep, **({"n": c["n"], "at": now, "wakes": int((r or {}).get("wakes", 0)) + 1}
                                                     if why else {})}
        if why:
            due.append((c, why))
    for key in [k for k, r in st["raised"].items() if k not in seen and r.get("kind") in kinds]:
        r = st["raised"][key]
        r["ok_n"] = int(r.get("ok_n") or 0) + 1
        r["ok_since"] = r.get("ok_since") or now
        if key not in gone and (r["ok_n"] < s["clear_n"] or now - float(r["ok_since"]) < s["clear_min"] * 60):
            continue
        _unraise(st, key, now)
    return due


def _by_time(c: dict) -> bool:
    """A probe stall: its count is one verdict per probe interval, so it grows with time alone and
    never escalates; it wakes again only at rewake_h."""
    return c["kind"] == "stall" and str(c["key"]).startswith("probe:")


def _unraise(st: dict, key: str, now: float) -> None:
    """Clear a raised key into `history`."""
    r = st["raised"].pop(key, None)
    if r is not None:
        st["history"].append({"key": key, "kind": r.get("kind"), "what": r.get("what"), "raised": r.get("first"),
                              "cleared": r.get("ok_since") or now, "wakes": r.get("wakes", 0)})


def check(db: DB, cfg: dict | None, state_dir: Path | str | None, now: float | None = None, where: Any = None,
          current_probe: Callable[[dict], str | None] | None = None, wake: bool = True,
          changes: list[dict] | None = None) -> list[dict]:
    """One pass: read drivers' results, find every stuck condition, raise one event per kind for the
    keys due a wake (muted keys only counted) and clear the keys healthy long enough. `changes`: this
    project's open machine-ledger changes (None: change-probe keys are not judged). Returns the
    events raised."""
    now = time.time() if now is None else now
    cfg = cfg or {}
    s = check_settings(cfg)
    if current_probe is None:
        from .daemon import _current_probe as current_probe
    st = load(db)
    recs = entries(Path(state_dir)) if state_dir else []
    _take_results(st, recs, now)
    gone: set = set()
    harness = getattr(where, "harness", None)
    conds = (no_progress(db, cfg, s, st, now, streaks(recs))
             + probe_stalls(db, s, st, now, current_probe, cfg, gone, harness)
             + run_stalls(db, s, now) + schedule_stalls(db, s, st, now))
    kinds = CHECK_KINDS
    if changes is None:
        kinds = tuple(k for k in CHECK_KINDS if k != "change-probe")
    else:
        got, ended = change_probes(db, s, st, changes, now)
        conds, gone = conds + got, gone | ended
    due = _decide(st, conds, s, now, kinds, gone)
    with db.tx():
        _save(db, st)
        return _emit(db, due, now, where, wake)


def change_probes(db: DB, s: dict, st: dict, changes: list[dict], now: float) -> tuple[list[dict], set[str]]:
    """Open machine changes of this project (`changes`, the ledger's) whose until-probe passed
    `change_probe_m` minutes ago while the task that made the change, or its until-task, is still
    queued or waiting: the change is due its undo, and its owner sleeps. The ledger pages the
    coordinator that the change is overdue but wakes no task. Also returns the raised keys whose
    condition ended: the change closed or its task no longer waits."""
    from . import ends
    out, gone, open_ids = [], set(), set()
    for c in changes:
        open_ids.add(str(c.get("id")))
        try:
            tid = int(c.get("task") or c.get("until_task") or 0)
        except (TypeError, ValueError):
            tid = 0
        t = db.task(tid) if tid else None
        if not t:
            continue
        if t["status"] not in ("queued", "waiting"):
            gone.add(f"change-probe:{c.get('id')}")
            continue
        passed = c.get("probe_passed") if not ends.always_succeeds(c.get("probe") or "") else None
        if not s["change_probe_m"] or not passed or now - float(passed) < s["change_probe_m"] * 60:
            continue
        out.append({"kind": "change-probe", "key": str(c.get("id")), "task": tid, "n": 1, "since": float(passed),
                    "what": f"machine change #{c.get('id')} on {c.get('alias')} ({_clip(c.get('what'), 60)}), "
                            f"owned by task #{tid} ({t['status']})",
                    "reason": f"its until-probe passed; undo `{_clip(c.get('undo'), 80)}` and close it, "
                              f"or wake task #{tid}"})
    gone |= {k for k, r in st["raised"].items() if r.get("kind") == "change-probe"
             and k.split(":", 1)[1] not in open_ids}
    return out, gone


def _emit(db: DB, due: list[tuple[dict, str]], now: float, where: Any, wake: bool) -> list[dict]:
    """One queued event per event kind for the keys due a wake (muted keys only counted)."""
    from . import screen as scr
    events = []
    for kind in EVENT_KINDS:
        items = []
        for c, why in due:
            if EVENTS.get(c["kind"], c["kind"]) != kind:
                continue
            text = _line(c, now, where) + ("" if why == "new" else f" ({why})")
            if scr.count_muted(db, SOURCE, text, "normal", now, whole=True, key=c["key"]):
                continue
            items.append((c, text))
        if not items or not wake:
            continue
        text = (HEADS[kind] + " Re-plan, re-tier, re-point the probe, route around, cancel, or add a harness task "
                "for the cause:\n" + "\n".join(f"- {t}" for _, t in items))
        fp = f"{kind}:" + ",".join(_key(c) for c, _ in items)
        task = items[0][0].get("task") if len(items) == 1 else None
        data = json.dumps([{k: c.get(k) for k in ("key", "what", "since", "last", "n", "reason", "selfref")}
                           for c, _ in items])
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) "
             "VALUES(?,?,?,?,?,?,?,?,?)", (now, SOURCE, kind, fp[:300], "normal", text[:4000], data, "queued", task))
        events.append({"kind": kind, "keys": [_key(c) for c, _ in items], "text": text})
    return events


# ---- digest and daily review ----

def digest_lines(db: DB, now: float | None = None) -> tuple[list[str], str]:
    """The coordinator's Progress block ([] when nothing is stuck) and its identity (keys only)."""
    now = time.time() if now is None else now
    raised = load(db)["raised"]
    if not raised:
        return [], ""
    lines = [HEADER]
    for key, r in sorted(raised.items(), key=lambda kv: float(kv[1].get("since") or now)):
        lines.append(f"- {r.get('kind')} [{key.split(':', 1)[1]}] {_clip(r.get('what'), 100)}: stuck "
                     f"{_dur(now - float(r.get('since') or now))}; why: {_clip(r.get('reason'), 140)}"
                     + (f"; {r['selfref']}" if r.get("selfref") else "")
                     + (f"; healthy for {_dur(now - float(r['ok_since']))}, not cleared yet" if r.get("ok_since") else ""))
    return lines, "\n".join(sorted(raised))


def audit_line(db: DB, since: float, now: float | None = None) -> str:
    """The daily review's line: no_progress and stall keys raised or cleared in the window, how long
    each took to clear, and those still open."""
    now = time.time() if now is None else now
    st = load(db)
    cleared = [h for h in st["history"] if float(h.get("cleared") or 0) >= since]
    open_ = [(k, r) for k, r in st["raised"].items()]
    if not cleared and not open_:
        return ""
    parts = [f"{h['key']} cleared after {_dur(float(h['cleared']) - float(h.get('raised') or h['cleared']))}"
             f" ({h.get('wakes', 0)} wake{'s' if h.get('wakes', 0) != 1 else ''})" for h in cleared[-8:]]
    parts += [f"{k} still open after {_dur(now - float(r.get('first') or now))}" for k, r in open_[:8]]
    return (f"no-progress and stall events: {len(cleared)} cleared, {len(open_)} open: " + "; ".join(parts))
