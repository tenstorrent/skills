"""Park, cancel and resume a queued checks run, and list a project's queue (`ttp checks park|cancel|resume|queue`).

A run waiting for a checks lane or slot (cli.CHECKS_SLOT_WAIT, written by the waiting process) may be parked:
PARK in its folder, for that waiter's pid and start, makes the waiter let go of every queue place (Parked,
raised in its wait) and wait outside the queue holding nothing, until the file goes (`resume`), its `--until`
probe passes (a read-only shell check run in the project root every PROBE_S) or a cancel comes. It then queues
again with its first arrival, so it keeps its place. A cancel writes cli.CHECKS_SLOT_CANCEL: the waiter drops
the run (exit 1), its task stays open with its commits and worktree, and runs its checks again when it next
needs them. Neither spends an attempt. Each action is logged with who, why and when in the run folder (LOG,
progress.md), the project's events (EVENT, handled: it wakes nobody) and the waiter's CHECKS_SLOT_WAIT, which
the task's `--result` probe prints as its note.

Who may: the coordinator, the user at a terminal (no run of any project above it) and a worker whose own task
is urgent or priority 1 and whose spec gives the order (one paragraph naming the action itself, `ttp checks
park|cancel` or park or cancel before checks runs, with a user msg # or the coordinator). Refused: a run holding a slot (only waiting runs are parked or cancelled; a review precheck
no review reads any more may be cancelled anyway: jobstop.may_stop), a push batch's checks, a run folder
outside this project, a precheck an open review still reads, and any other caller."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PARK = "checks-slot.park"   # in the run dir: {pid, started, who, why, until, at}
LOG = "checks-queue.jsonl"   # in the run dir: one line per park, cancel and resume
EVENT = "checks_queue"
PROBE_S = 60   # how often a parked run asks its --until probe
PROBE_TIMEOUT_S = 50
POLL_S = 2.0
ACTIONS = ("park", "cancel", "resume", "queue")
# The checks-queue action itself: `ttp checks park|cancel`, or the word park or cancel (no variants such as
# 'parked' or 'cancelling', which daemon notes about tasks carry) within a few words before checks or
# queued runs ("park the routine checks runs", "cancel the queued runs").
_ORDER = re.compile(r"(?i)\bttp\s+checks\s+(park|cancel)\b"
                    r"|\b(park|cancel)\b(\s+[\w#'-]+){0,3}?\s+(checks?|queued)\s+runs?\b")
_CITE = re.compile(r"(?i)(\bmsg #\d+|\bcoordinator)")


class Parked(Exception):
    """Raised in a checks wait whose run was parked: the wait lets go of all it holds."""


def _json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def request(run_dir: Path) -> dict:
    """The park request for this process (PARK naming its pid and start), else {}."""
    from .runner import proc_start
    req = _json(Path(run_dir) / PARK)
    if req.get("pid") != os.getpid() or req.get("started") != proc_start(os.getpid()):
        return {}
    return req


def note(req: dict) -> str:
    """The waiting task's line for a park request."""
    return (f"checks run parked by {req.get('who') or '?'}: {req.get('why') or 'no reason given'}; resumes when "
            + (f"`{req['until']}` passes" if req.get("until") else "`ttp checks resume` is run"))


def log(p, run_dir: Path, action: str, who: str, why: str, task, **extra) -> dict:
    """Record one action in the run folder, its notes and the project's events."""
    from . import timefmt
    from .project import durable_append
    rec = {"ts": time.time(), "action": action, "who": who, "why": why, "run": Path(run_dir).name,
           **({"task": int(task)} if task else {}), **extra}
    durable_append(Path(run_dir) / LOG, json.dumps(rec) + "\n")
    durable_append(Path(run_dir) / "progress.md", f"{timefmt.clock()} checks: {action} by {who}: {why}\n")
    if p is not None:
        try:
            p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
                   (rec["ts"], "checks", EVENT, "info", f"checks run {rec['run']} {action} by {who}: {why}"[:500],
                    json.dumps(rec), "handled", int(task) if task else None))
        except Exception as e:   # the run folder's log above keeps it
            print(f"ttp checks: event not recorded: {e}", file=sys.stderr)
    return rec


def hold(p, run_dir: Path, head: str, req: dict, cancelled, write_wait) -> str:
    """Wait parked, holding no queue place: "" once resumed (PARK gone or its probe passed), else the cancel's
    why. `cancelled()` names a cancel request for this process; `write_wait(dict)` records the wait."""
    write_wait({"since": time.time(), "pid": os.getpid(), "head": head, "parked": req, "note": note(req)})
    asked = 0.0
    root = str(p.root) if p is not None else None
    while True:
        if why := cancelled():
            return why
        if not request(run_dir):
            return ""
        if req.get("until") and time.time() - asked >= PROBE_S:
            asked = time.time()
            try:
                ok = subprocess.run(["sh", "-c", str(req["until"])], cwd=root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=PROBE_TIMEOUT_S).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            if ok:
                (Path(run_dir) / PARK).unlink(missing_ok=True)
                log(p, run_dir, "resume", "probe", f"`{req['until']}` passed", os.environ.get("TTP_TASK"))
                return ""
        time.sleep(POLL_S)


def caller(p, action: str) -> tuple[str, str]:
    """(who, "") for a caller allowed to `action`, else ("", why it is refused)."""
    from . import coordtools, jobstop, locks
    task, run = os.environ.get("TTP_TASK") or "", os.environ.get("TTP_RUN_ID") or ""
    if not task and os.environ.get(coordtools.ROLE_ENV) == "coordinator":
        if run.isdigit() and not p.db.one("SELECT id FROM runs WHERE id=? AND role='coordinator'", (int(run),)):
            return "", f"refused: run {run} is no coordinator run of this project"
        return f"the coordinator (run {run or '?'})", ""
    if not task and not run and not coordtools.coordinator_run() and not locks.under_run_agent():
        return "the user", ""
    tid = jobstop.caller_task(p, task, run)
    if tid is None:
        return "", "refused: the caller is no task run of this project (TTP_TASK and TTP_RUN_ID do not match)"
    t = p.db.task(tid) or {}
    try:
        from . import urgent
        prio = min(int(t.get("priority") if t.get("priority") is not None else 9),
                   urgent.queue_priority(t) if urgent.queue_priority(t) is not None else 9)
    except Exception:
        prio = 9
    if prio > 1:
        return "", f"refused: task #{tid} is not urgent or priority 1"
    if action != "resume" and not ordered(t.get("spec") or ""):
        return "", (f"refused: task #{tid}'s spec gives no order to park or cancel checks runs (a paragraph "
                    "naming `ttp checks park|cancel`, or park or cancel before checks runs, with a user msg # or the "
                    "coordinator)")
    return f"task #{tid} (run {run})", ""


def ordered(spec: str) -> bool:
    """One paragraph of the spec names the checks-queue action itself (_ORDER) together with a user msg # or
    the coordinator."""
    return any(_ORDER.search(par) and _CITE.search(par) for par in re.split(r"\n\s*\n", spec))


def waiter(run_dir: Path) -> dict:
    """The live checks of the run folder while they wait for a lane or slot (its CHECKS_SLOT_WAIT names their
    pid), else {}; {"held": True, ...} while detached checks run holding one. Checks run in the foreground
    (no live CHECKS_PID) count by their CHECKS_SLOT_WAIT pid alone."""
    from .cli import CHECKS_SLOT_WAIT, _checks_alive, _read_checks_pid
    from .runner import proc_start
    info = _read_checks_pid(run_dir)
    wait = _json(Path(run_dir) / CHECKS_SLOT_WAIT)
    if not _checks_alive(info):
        pid = wait.get("pid")
        started = proc_start(pid) if isinstance(pid, int) else None
        return {"pid": pid, "started": started, "wait": wait} if started else {}
    if wait.get("pid") != info.get("pid"):
        return {**info, "held": True}
    return {**info, "wait": wait}


def push_batch(info: dict) -> bool:
    """The checks run for a push to the push branch (TTP_PUSH_MODE `target`, or a landing mark, in their
    environment); `ttp checks` sets mode `checks` for its own commands, so a run inside one is no push batch."""
    from . import landmark, procowner
    env = procowner.environ(info["pid"]) if isinstance(info.get("pid"), int) else None
    return bool(env and (env.get("TTP_PUSH_MODE") == "target" or env.get(landmark.ENV)))


def _kind(p, d: Path) -> tuple[str, int | None]:
    """("run" | "precheck" | "", task id) of a run folder of this project."""
    from . import jobstop
    if (tid := jobstop.precheck_task(p, d)) is not None:
        return "precheck", tid
    if d.parent == Path(os.path.realpath(p.runs)):
        r = p.db.one("SELECT task FROM runs WHERE id=?", (int(d.name),)) if d.name.isdigit() else None
        return ("run", r["task"]) if r and r["task"] else ("", None)
    return "", None


def resolve(p, target: str, task: str, action: str) -> list[Path]:
    """The run folders `target` (a run id, a precheck t<task>-<head> or precheck-t..., or a folder) or
    `--task` names: for --task, its runs whose checks wait (resume: are parked). ValueError when none is
    this project's."""
    if task:
        if not str(task).isdigit():
            raise ValueError(f"--task {task}: not a task id")
        want = PARK if action == "resume" else None
        out = []
        for r in p.db.q("SELECT dir FROM runs WHERE task=? AND dir IS NOT NULL ORDER BY id", (int(task),)):
            d = Path(os.path.realpath(r["dir"]))
            if (want and (d / want).exists()) or (not want and waiter(d).get("wait")):
                out.append(d)
        pre = p.state / "prechecks"
        for d in sorted(pre.glob(f"t{int(task)}-*")) if pre.is_dir() else []:
            if (want and (d / want).exists()) or (not want and waiter(d).get("wait")):
                out.append(Path(os.path.realpath(d)))
        if not out:
            raise ValueError(f"task #{task} has no checks run that " + ("is parked" if want else "waits in the queue"))
        return out
    t = str(target or "")
    if t.isdigit():
        d = p.runs / t
    elif m := re.fullmatch(r"(?:precheck-)?(t\d+-[0-9a-f]{7,40})", t):
        d = p.state / "prechecks" / m.group(1)
    else:
        d = Path(t)
    d = Path(os.path.realpath(d))
    if not d.is_dir() or not _kind(p, d)[0]:
        raise ValueError(f"refused: {t} is no checks run of project {p.name} (another project's, a push "
                         "batch's or no run folder)")
    return [d]


def act(p, d: Path, action: str, who: str, why: str, until: str) -> tuple[int, str]:
    """Park, cancel or resume the checks of run folder `d`: (0, what) or (3, why refused)."""
    from .cli import CHECKS_SLOT_CANCEL
    from .project import write_json
    kind, task = _kind(p, d)
    info = waiter(d)
    if action == "resume":
        req = _json(d / PARK)
        if not req:
            return 3, f"run {d.name}: not parked"
        (d / PARK).unlink(missing_ok=True)
        log(p, d, "resume", who, why or "resumed", task)
        return 0, f"run {d.name}: resumed; it queues again at its first arrival"
    if info and push_batch(info):
        return 3, f"refused: run {d.name} checks a push batch"
    if kind == "precheck":
        from . import jobstop
        if no := jobstop.precheck_read(p, d):
            return 3, f"refused: {no}"
        if action == "cancel" and info.get("held"):   # past its review's verdict: stopped as `ttp detach --stop` does
            code, text, _ = jobstop.stop_checks(p, d, os.environ.get("TTP_TASK"), os.environ.get("TTP_RUN_ID"),
                                                f"cancelled by {who}: {why}")
            if code != 3:
                log(p, d, "cancel", who, why, task)
            return (0 if code == 0 else 3 if code == 3 else 1), text
    if not info:
        return 3, f"run {d.name}: its checks no longer run; nothing to {action}"
    if info.get("held"):
        return 3, f"refused: run {d.name}'s checks hold a slot (only a run waiting in the queue is parked or cancelled)"
    req = {"pid": info["pid"], "started": info["started"], "who": who, "why": why, "at": time.time()}
    if action == "park":
        write_json(d / PARK, {**req, **({"until": until} if until else {})})
        log(p, d, "park", who, why, task, **({"until": until} if until else {}))
        return 0, f"run {d.name}: parked; " + note({**req, "until": until})
    write_json(d / CHECKS_SLOT_CANCEL, {"pid": info["pid"], "started": info["started"],
                                         "why": f"cancelled by {who}: {why}"})
    log(p, d, "cancel", who, why, task)
    return 0, (f"run {d.name}: cancelled; it leaves the queue within seconds, its task keeps its commits and "
               "worktree and runs its checks again when it next needs them")


def queued(p) -> list[dict]:
    """This project's checks runs that wait in the queue or are parked, earliest arrival first."""
    from . import checkcap, obligations
    dirs = [Path(os.path.realpath(r["dir"])) for r in p.db.q(
        "SELECT r.dir FROM runs r JOIN tasks t ON t.id=r.task WHERE r.dir IS NOT NULL AND t.status NOT IN "
        "('done','failed','cancelled','exhausted') ORDER BY r.id")]
    pre = p.state / "prechecks"
    dirs += [Path(os.path.realpath(d)) for d in sorted(pre.iterdir())] if pre.is_dir() else []
    out = []
    for d in dirs:
        info = waiter(d)
        if not info.get("wait"):
            continue
        kind, task = _kind(p, d)
        t = p.db.task(task) if task else None
        owed = bool(task and obligations.owned_open(p.db, int(task)))
        out.append({"run": d.name, "task": task, "priority": t.get("priority") if t else None,
                    "urgent": bool(t and checkcap.is_urgent(t.get("priority"))), "owed": owed,
                    "kind": "push batch" if push_batch(info) else ("review precheck" if kind == "precheck" else "task"),
                    "since": arrival(info), "parked": note(info["wait"]["parked"])
                    if isinstance(info["wait"].get("parked"), dict) else "", "pid": info.get("pid"),
                    "holds": _holds(info["wait"])})
    return sorted(out, key=lambda e: e.get("since") or 0)


def arrival(info: dict) -> float | None:
    """When a waiting run first joined the queue: the earliest of its checks.pid 'arrived' (detached checks),
    the arrival a foreground run was handed (checkcap.ARRIVED_ENV in its environment) and its latest
    CHECKS_SLOT_WAIT 'since', which a re-wait resets."""
    from . import checkcap, procowner
    seen = [info.get("arrived"), info["wait"].get("since")]
    env = procowner.environ(info["pid"]) if isinstance(info.get("pid"), int) else None
    seen.append((env or {}).get(checkcap.ARRIVED_ENV))
    now = time.time()
    good = []
    for t in seen:
        try:
            t = float(t)
        except (TypeError, ValueError):
            continue
        if 0 < t <= now:
            good.append(t)
    return min(good) if good else None


def _holds(wait: dict) -> str:
    """What holds a waiting run, from its CHECKS_SLOT_WAIT `wait`: parked, following another run, its suite's
    test slots, else a checks slot here (a lane or the host-wide slot, with the load guard)."""
    if isinstance(wait.get("parked"), dict):
        return f"parked: {note(wait['parked'])}"
    if wait.get("follows"):
        return f"follows another run of its task: {str(wait['follows'])[:200]}"
    if wait.get("suite"):
        return f"the test slots of suite {wait['suite']}" + (f" (limit {wait['limit']})" if wait.get("limit") else "")
    return "a checks slot here (its lane or the host-wide slot, with the load guard)"


def live_runs(repos: list[dict], spilled: list[dict], waiting: list[dict]) -> list[dict]:
    """`ttp checks status`'s one list of every live check run of the project: each lane holder running here,
    each run on a spill host (checkspill.holders: host and pid) and each waiting one, a lane's ticket or a run
    waiting for a slot (queued), with what holds it. One entry per run; empty only when nothing runs or waits."""
    out, seen = [], set()

    def add(e: dict) -> None:
        key = ("run", e["run"]) if e.get("run") else ("pid", e.get("pid"))
        if key in seen:
            return
        seen.add(key)
        out.append(e)
    for c in spilled:
        add({"state": "spilled", "task": c.get("task") or None, "run": str(c.get("run") or ""), "pid": c.get("pid"),
             "host": c.get("host"), "since": c.get("since"), "what": c.get("what") or ""})
    for g in repos:
        for h in g.get("lanes") or ():
            run = not h.get("waits_on")   # a holder not started yet names what it waits on (lane_status)
            add({"state": "running" if run else "waiting", "task": h.get("task") or None, "run": str(h.get("run") or ""),
                 "pid": h.get("pid"), "lane": g.get("label"), "what": h.get("command") or h.get("why") or "",
                 **({} if run else {"holds": h.get("waits_on") or f"lane {g.get('label')}: not started"})})
        for w in g.get("waiters") or ():
            add({"state": "waiting", "task": w.get("task") or None, "run": str(w.get("run") or ""), "pid": w.get("pid"),
                 "lane": g.get("label"), "position": w.get("position"),
                 "holds": w.get("waits_on") or f"lane {g.get('label')}, place {w.get('position')}"})
    for w in waiting:
        add({"state": "waiting", "task": w.get("task"), "run": str(w.get("run") or ""), "pid": w.get("pid"),
             "since": w.get("since"), "kind": w.get("kind"), "holds": w.get("holds") or ""})
    return out


def live_runs_lines(runs: list[dict]) -> list[str]:
    """The text lines of live_runs: a count, then one line per run."""
    if not runs:
        return ["live check runs: none running or waiting"]
    n = {s: sum(1 for r in runs if r["state"] == s) for s in ("running", "spilled", "waiting")}
    lines = [f"live check runs: {n['running']} running here, {n['spilled']} on spill hosts, {n['waiting']} waiting"]
    for r in runs:
        who = " ".join(x for x in (f"#{r['task']}" if r.get("task") else "no task",
                                   f"run {r['run']}" if r.get("run") else "", f"pid {r['pid']}" if r.get("pid") else "") if x)
        lines.append(f"  {r['state']}: {who}" + (f" on {r['host']}" if r.get("host") else "")
                     + (f", held by {r['holds']}" if r.get("holds") else ""))
    return lines


def main(p, argv: list[str]) -> int:
    """`ttp checks park|cancel|resume <run> --reason ... [--until ...] [--task <id>]` and `ttp checks queue`."""
    import argparse
    ap = argparse.ArgumentParser(prog=f"ttp checks {argv[0]}", allow_abbrev=False)   # coordtools.queue_refusal reads the full names
    ap.add_argument("run", nargs="?", default="")
    ap.add_argument("--task", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv[1:])
    action = argv[0]
    if action == "queue":
        rows = queued(p)
        if a.json:
            print(json.dumps(rows, indent=1))
        for e in [] if a.json else rows:
            print(f"run {e['run']}  task #{e['task'] or '-'}  priority {e['priority']}"
                  f"{' urgent' if e['urgent'] else ''}{' owed' if e['owed'] else ''}  {e['kind']}"
                  f"  waiting {int(time.time() - (e['since'] or time.time()))}s{'  ' + e['parked'] if e['parked'] else ''}")
        if not rows and not a.json:
            print(f"no checks run of project {p.name} waits in the queue")
        return 0
    if not (a.run or a.task) or (a.run and a.task):
        print(f"ttp checks {action}: give a run or --task <id> (one of them)", file=sys.stderr)
        return 2
    if action != "resume" and not a.reason.strip():
        print(f"ttp checks {action}: --reason is required", file=sys.stderr)
        return 2
    if a.until and action != "park":
        print("ttp checks: --until is for park only", file=sys.stderr)
        return 2
    who, no = caller(p, action)
    if no:
        print(f"ttp checks {action}: {no}", file=sys.stderr)
        return 3
    try:
        dirs = resolve(p, a.run, a.task, action)
    except ValueError as e:
        print(f"ttp checks {action}: {e}", file=sys.stderr)
        return 3 if str(e).startswith("refused") else 1
    worst = 0
    for d in dirs:
        code, text = act(p, d, action, who, a.reason.strip(), a.until.strip())
        print(text if code == 0 else f"ttp checks {action}: {text}", file=sys.stdout if code == 0 else sys.stderr)
        worst = max(worst, code)
    return worst
