# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One suite per task, head and commands: `ttp checks` joins its task's same checks already queued.

Each full `ttp checks` run of a task records itself in the project's state (FOLDER/<id>.json): task,
head, the commit's time, its key (task, head, commands, purpose and environment), its group (the same
without the head, plus the repository), pid, arrival and phase (waiting, running, joined, superseded).
A run that is still waiting looks at the others (target) and stops waiting when

- the same task's same checks on the same head wait ahead of it or run now: it joins them;
- the same task queued the same checks on a newer head (a later commit time): it is superseded.

Either way it leaves the queue, follows that run (through any run it joined in turn) and reports that
run's outcome (finish) as its own: passed, failed or timed out, naming the head that ran, never a pass
of its own head. A run that has started is never stopped: only waiters look. A different command,
environment, purpose, repository or task is never a duplicate. When the followed run ends without an
outcome (killed, handed over, cancelled), the follower queues again. `ttp checks status` and `ttp status`
list the followers (status_lines), so they are not taken for lost runs."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

FOLDER = "checks-joins"
KEEP_S = 6 * 3600.0     # a record with an outcome stays this long for its followers
DEPTH = 8               # the longest chain of followers resolved
WAITING, RUNNING, JOINED, SUPERSEDED = "waiting", "running", "joined", "superseded"


class Yield(Exception):
    """Raised out of a slot wait: this run stops waiting and follows `target` (`how`: joined or superseded)."""

    def __init__(self, target: dict, how: str):
        super().__init__(how)
        self.target, self.how = target, how


def _digest(*parts) -> str:
    return hashlib.sha256(json.dumps([str(x) for x in parts]).encode()).hexdigest()[:24]


def key(task: str, head: str, cmds_hash: str, purpose: str, envs) -> str:
    """What makes two runs the same checks: task, head, commands, purpose and environment."""
    return _digest("key", task, head, cmds_hash, purpose, json.dumps(envs or []))


def group(task: str, repo: str, cmds_hash: str, purpose: str, envs) -> str:
    """The same checks of one task in one repository, whatever the head: a newer head supersedes."""
    return _digest("group", task, repo, cmds_hash, purpose, json.dumps(envs or []))


def folder(state: Path) -> Path:
    return Path(state) / FOLDER


def _write(path: Path, rec: dict) -> None:
    from .project import durable_write
    durable_write(path, json.dumps(rec))


def _pid_ns() -> str:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


def register(state: Path, *, task: str, head: str, commit_ts: float, key: str, group: str, run: str,
             arrived: float, pid: int | None = None, now: float | None = None) -> Path:
    """This run's record, phase waiting. Raises OSError when the state folder cannot be written."""
    from .runner import proc_start
    pid = os.getpid() if pid is None else pid
    d = folder(state)
    d.mkdir(parents=True, exist_ok=True)
    rid = f"{pid}-{uuid.uuid4().hex[:12]}"
    path = d / f"{rid}.json"
    _write(path, {"id": rid, "pid": pid, "started": proc_start(pid), "pidns": _pid_ns(), "task": str(task),
                  "head": head, "commit_ts": float(commit_ts), "key": key, "group": group, "run": str(run),
                  "arrived": float(arrived), "ts": time.time() if now is None else now, "phase": WAITING})
    return path


def read(path: Path) -> dict | None:
    try:
        rec = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def update(path: Path | None, **fields) -> None:
    """Change this run's own record (only its own process writes it); a failed write is ignored."""
    if path is None or (rec := read(path)) is None:
        return
    try:
        _write(Path(path), {**rec, **fields})
    except OSError:
        pass


def alive(rec: dict) -> bool:
    """Whether the record's process still runs: same pid and start, in this PID namespace."""
    from .runner import proc_start
    pid = rec.get("pid")
    if not isinstance(pid, int) or (rec.get("pidns") or "") != _pid_ns():
        return False
    return rec.get("started") is not None and proc_start(pid) == rec.get("started")


def records(state: Path, now: float | None = None, prune: bool = True) -> list[dict]:
    """Every record, each with "alive". With `prune`, records of dead runs without an outcome and
    outcomes older than KEEP_S are removed."""
    now = time.time() if now is None else now
    out = []
    try:
        paths = sorted(folder(state).glob("*.json"))
    except OSError:
        return []
    for path in paths:
        rec = read(path)
        if rec is None:
            continue
        rec["alive"] = alive(rec)
        done = rec.get("outcome")
        stale = (not done and not rec["alive"]) or (done and now - float(done.get("ts") or 0) > KEEP_S)
        if stale and prune:
            path.unlink(missing_ok=True)
            continue
        out.append(rec)
    return out


def _order(r: dict) -> tuple:
    return (float(r.get("arrived") or 0), float(r.get("ts") or 0), str(r.get("id") or ""))


def target(me: dict, recs: list[dict]) -> tuple[dict, str] | None:
    """(the run this waiting run should follow, joined or superseded), or None to keep waiting.
    Superseded: the same task's group on another head with a later commit time, live or finished.
    Joined: the same key, waiting ahead of it or running now (a running one first)."""
    if not me.get("task"):
        return None
    others = [r for r in recs if r.get("id") != me.get("id") and r.get("task") == me.get("task")]
    newer = [r for r in others if r.get("group") == me.get("group")
             and r.get("head") != me.get("head") and float(r.get("commit_ts") or 0) > float(me.get("commit_ts") or 0)
             and (r.get("outcome") or r.get("alive"))]
    if newer:
        return max(newer, key=lambda r: (float(r.get("commit_ts") or 0), _order(r))), SUPERSEDED
    same = [r for r in others if r.get("key") == me.get("key") and r.get("alive") and not r.get("outcome")
            and r.get("phase") in (WAITING, RUNNING)]
    if running := [r for r in same if r.get("phase") == RUNNING]:
        return min(running, key=_order), JOINED
    if ahead := [r for r in same if _order(r) < _order(me)]:
        return min(ahead, key=_order), JOINED
    return None


def resolve(rid: str, recs: list[dict]) -> tuple[str, dict | None]:
    """Where the run `rid` stands, through the runs it follows: ("done", its record with the outcome),
    ("wait", the record still to end) or ("lost", None): it ended without an outcome or is gone. A run
    it follows that lost its own lead still runs and queues again itself: that is a wait, not lost."""
    by = {r.get("id"): r for r in recs}
    first = by.get(rid)
    for _ in range(DEPTH):
        r = by.get(rid)
        if r is None or not (r.get("outcome") or r.get("alive")):
            break
        if r.get("outcome"):
            return "done", r
        if r.get("phase") in (JOINED, SUPERSEDED) and r.get("follows"):
            rid = r["follows"]
            continue
        return "wait", r
    if first is not None and first.get("alive") and not first.get("outcome"):
        return "wait", first
    return "lost", None


def finish(path: Path | None, *, passed: bool, head: str, rc: int, failed: str = "", timed_out: bool = False,
           summary: str = "", log: str = "") -> None:
    """Record this run's outcome for its followers (a run that ran its checks, or followed another)."""
    update(path, outcome={"passed": bool(passed), "head": head, "rc": int(rc), "failed": failed,
                          "timed_out": bool(timed_out), "summary": summary[-4000:], "log": log, "ts": time.time()})


def said(how: str, mine: str, leader: dict) -> str:
    """The line a follower prints when it stops waiting."""
    who = f"run {leader.get('run') or '?'} (pid {leader.get('pid')})"
    if how == SUPERSEDED:
        return (f"superseded: this task queued the same checks on a newer head {str(leader.get('head'))[:12]} in "
                f"{who}; these on {mine[:12]} leave the queue unrun and report that run's result")
    return (f"joined: the same checks of this task on {mine[:12]} {'run' if leader.get('phase') == RUNNING else 'wait'} "
            f"in {who}; waiting for their result instead of running a second suite")


def result_line(how: str, mine: str, leader: dict) -> str:
    """The outcome a follower reports: the run it followed, never a pass of its own head."""
    o = leader.get("outcome") or {}
    ran = str(o.get("head") or leader.get("head") or "")
    what = ("passed" if o.get("passed") else "TIMED OUT" if o.get("timed_out")
            else f"FAILED ({o['failed']!r})" if o.get("failed") else "FAILED")
    base = f"run {leader.get('run') or '?'}'s checks {what} on {ran[:12]}"
    if how == SUPERSEDED:
        base += f"; {mine[:12]} itself was not checked (superseded by that newer head)"
    else:
        base += " (joined: no second suite ran)"
    return base + (f" (full output: {o['log']})" if o.get("log") else "")


def status_lines(state: Path, now: float | None = None) -> list[str]:
    """One line per live follower (joined or superseded), for `ttp checks status` and `ttp status`."""
    now = time.time() if now is None else now
    recs = records(state, now, prune=False)
    by = {r.get("id"): r for r in recs}
    out = []
    for r in recs:
        if not r.get("alive") or r.get("outcome") or r.get("phase") not in (JOINED, SUPERSEDED):
            continue
        lead = by.get(r.get("follows")) or {}
        who = f"#{r.get('task')} run {r.get('run') or '?'} pid {r.get('pid')}"
        waited = max(0.0, now - float(r.get("since") or r.get("ts") or now))
        if r.get("phase") == JOINED:
            out.append(f"  joined: {who} on {str(r.get('head'))[:12]} follows run {lead.get('run') or '?'} "
                       f"(same head and commands, {lead.get('phase') or 'ended'}), {int(waited // 60)} min; "
                       f"no second suite runs")
        else:
            out.append(f"  superseded: {who} on {str(r.get('head'))[:12]} by newer head "
                       f"{str(lead.get('head') or '?')[:12]} in run {lead.get('run') or '?'}, {int(waited // 60)} "
                       f"min; it reports that run's result")
    return out
