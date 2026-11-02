# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One suite per tree and commands: `ttp checks` joins the same checks already queued or running.

Each full `ttp checks` run of a task records itself in the project's state (FOLDER/<id>.json): task,
head, the commit's time, its key (repository, the head's tree, commands, purpose and environment), its
group (the task, repository, commands, purpose and environment, whatever the head), pid, arrival and
phase (waiting, running, joined, superseded). A run that is still waiting looks at the others (target)
and stops waiting when

- the same checks on the same tree wait ahead of it or run now, in any task of the project (a worker's
  own run, its detached rerun, the daemon's review precheck, a reviewer's run): it joins them;
- the same task queued the same checks on a newer head (a later commit time) that runs, waits or ended
  while this one waited: it is superseded (a newer head that ended before this run queued is not).

Either way it leaves the queue, follows that run (through any run it joined in turn) and reports that
run's outcome (finish) as its own: passed, failed or timed out. A joined run reports it for its own
head (the same tree ran), a superseded one names the newer head that ran, never a pass of its own head.
A joined run whose outcome is of another tree (the run it joined was superseded; ran_key) queues again.
A run that has started is never stopped: only waiters look. A different tree, command, environment,
purpose or repository is never a duplicate. A failed outcome is only passed to the runs that joined
while it ran: a later request for the same key runs again (only passes are recorded and reused). When the followed run ends without an
outcome (killed, handed over, cancelled), the follower queues again. `ttp checks status` and `ttp status`
list the followers (status_lines), so they are not taken for lost runs."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from .project import ps_snapshot

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


def key(repo: str, tree: str, cmds_hash: str, purpose: str, envs) -> str:
    """What makes two runs the same checks, whichever task asks: repository, tree, commands, purpose and
    environment (pushpass.check_envs: interpreter, dependencies, the variables a check reads)."""
    return _digest("key", repo, tree, cmds_hash, purpose, json.dumps(envs or []))


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


@ps_snapshot()   # one `ps` for every record's pid where there is no /proc
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


def forget(state: Path, pid: int | None = None) -> int:
    """Remove the records of the live process `pid` (default this one) that have no outcome, before it
    re-executes as the same pid (cli._checks_reexec): left behind, its old record would look alive and
    the re-executed run, registered again at the same arrival, would join it and wait for ever. Its
    followers then find their lead gone and queue again (behind it). Returns how many it removed."""
    pid = os.getpid() if pid is None else pid
    n = 0
    for rec in records(state, prune=False):
        if rec.get("pid") == pid and rec.get("alive") and not rec.get("outcome") and rec.get("id"):
            try:
                (folder(state) / f"{rec['id']}.json").unlink(missing_ok=True)
                n += 1
            except OSError:
                pass
    return n


def _order(r: dict) -> tuple:
    return (float(r.get("arrived") or 0), float(r.get("ts") or 0), str(r.get("id") or ""))


def _while_waiting(r: dict, me: dict) -> bool:
    """Whether run r was live while `me` waited: no outcome yet and alive, or an outcome recorded after
    `me` registered (it ran and ended between two polls). One that ended before `me` queued is not."""
    if done := r.get("outcome"):
        return float(done.get("ts") or 0) > float(me.get("ts") or 0)
    return bool(r.get("alive"))


def target(me: dict, recs: list[dict]) -> tuple[dict, str] | None:
    """(the run this waiting run should follow, joined or superseded), or None to keep waiting.
    Superseded: the same task's group on another head with a later commit time, live now or ended
    while this run waited (never one that ended before it queued: that older head runs its own checks).
    Joined: the same key (any task's), waiting ahead of it or running now (a running one first)."""
    if not me.get("task"):
        return None
    every = [r for r in recs if r.get("id") != me.get("id")]
    others = [r for r in every if r.get("task") == me.get("task")]
    newer = [r for r in others if r.get("group") == me.get("group")
             and r.get("head") != me.get("head") and float(r.get("commit_ts") or 0) > float(me.get("commit_ts") or 0)
             and _while_waiting(r, me)]
    if newer:
        return max(newer, key=lambda r: (float(r.get("commit_ts") or 0), _order(r))), SUPERSEDED
    same = [r for r in every if r.get("key") == me.get("key") and r.get("alive") and not r.get("outcome")
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
           summary: str = "", log: str = "", ran_key: str = "") -> None:
    """Record this run's outcome for its followers (a run that ran its checks, or followed another;
    `ran_key`: the key of the run that ran them, when not this run's own)."""
    update(path, outcome={"passed": bool(passed), "head": head, "rc": int(rc), "failed": failed,
                          "timed_out": bool(timed_out), "summary": summary[-4000:], "log": log, "ts": time.time(),
                          **({"key": ran_key} if ran_key else {})})


def ran_key(rec: dict) -> str:
    """The key of the checks whose outcome `rec` holds: the one recorded with it, else its own key (a
    superseded run's without one is another tree's, never its own: "")."""
    o = rec.get("outcome") or {}
    if o.get("key"):
        return str(o["key"])
    return "" if rec.get("phase") == SUPERSEDED else str(rec.get("key") or "")


def said(how: str, mine: str, leader: dict) -> str:
    """The line a follower prints when it stops waiting."""
    who = f"run {leader.get('run') or '?'} of #{leader.get('task') or '?'} (pid {leader.get('pid')})"
    if how == SUPERSEDED:
        return (f"superseded: this task queued the same checks on a newer head {str(leader.get('head'))[:12]} in "
                f"{who}; these on {mine[:12]} leave the queue unrun and report that run's result")
    return (f"joined: the same checks on the tree of {mine[:12]} {'run' if leader.get('phase') == RUNNING else 'wait'} "
            f"in {who}; waiting for their result instead of running a second suite")


def result_line(how: str, mine: str, leader: dict) -> str:
    """The outcome a follower reports: the run it followed; a superseded one's is never a pass of its own head."""
    o = leader.get("outcome") or {}
    ran = str(o.get("head") or leader.get("head") or "")
    what = ("passed" if o.get("passed") else "TIMED OUT" if o.get("timed_out")
            else f"FAILED ({o['failed']!r})" if o.get("failed") else "FAILED")
    base = f"run {leader.get('run') or '?'}'s checks {what} on {ran[:12]}"
    if how == SUPERSEDED:
        base += f"; {mine[:12]} itself was not checked (superseded by that newer head)"
    else:
        base += (f", the same tree as {mine[:12]}" if ran and not ran.startswith(mine[:12]) else "") + \
            " (joined: no second suite ran)"
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
                       f"of #{lead.get('task') or '?'} (same tree and commands, {lead.get('phase') or 'ended'}), "
                       f"{int(waited // 60)} min; "
                       f"no second suite runs")
        else:
            out.append(f"  superseded: {who} on {str(r.get('head'))[:12]} by newer head "
                       f"{str(lead.get('head') or '?')[:12]} in run {lead.get('run') or '?'}, {int(waited // 60)} "
                       f"min; it reports that run's result")
    return out
