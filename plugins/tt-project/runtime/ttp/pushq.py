# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The daemon's push queue (`delivery.push_queue`). A review that passes hands off the commits it
approves (`"push": [{"branch", "head"}]`) instead of running `ttp push`. The daemon pins each one at
refs/ttp/push/<id>, starts one detached `ttp push --batch <marker>` for several approvals, and
closes the reviews from what that process writes into its marker. None of it runs a model.

Rows in push_queue are the source of truth; each batch has a push_batches row and a marker under
state/pushes/. A review task waits in the status `pushing` while its rows are in the queue."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import locks, push
from .db import (TERMINAL_TASK_STATES, continues_id, dependency_ids, dump_result, load_result, review_subject,
                 reviews_task)
from .project import Project, nice_level, push_allowed, push_queue_on, renice, write_json

REF_PREFIX = "refs/ttp/push/"   # + row id: pins the approved commit until its row is settled
KV = "push_queue"               # kv: {"backoff_until", "deaths", "hold": {"tip", "rows", "until"}, "tips_told"}
DEFAULT_BATCH_S = 900           # delivery.push_batch_s: the oldest approval waits at most this long
DEFAULT_BATCH_MAX = 8           # delivery.push_batch_max: start at once with this many; also a batch's most
DEFAULT_MIN_GAP_S = 1800        # delivery.push_min_gap_s: the least time from a batch's end to the next start
HOLD_S = 1800                   # after tip_failed, the same tip and rows wait this long unless either changes
BACKOFF_STEP_S = 300            # a dead batch's rows wait 5 min per try ...
BACKOFF_MAX_S = 1800            # ... and at most 30 min
DYING_AFTER = 3                 # batches that die in a row before push_queue_dying is raised
MAX_RESUMES = 1                 # respawns of an after_push that died (a reboot killed the deploy)
RECOVER_S = 1800                # a batch that died mid-push: how long its remote may stay unreadable
RECOVER_GAP_S = 120             # ... asked at most this often meanwhile
RECOVER_FETCH_S = 20            # ... each fetch bounded, as the tick waits for it
REFUSAL_TIMEOUT_S = 30          # the approval asks the remote for its default branch; never hang the tick
DEFAULT_BRANCH_TTL_S = 3600     # ... and remembers the answer per remote this long (one ask, not one per approval)
DEFAULT_BRANCH_RETRY_S = 300    # an unreachable remote is asked again after this, not on every approval
AFTER_PUSH_OFF = "after_push_off"   # kv: true while after_push is unset or the queue is off (alerts.holds)
TIPS_TOLD = 20                  # tip_failed shas remembered, so each tip is reported once
ROW_RESULTS = ("pushed", "landed", "conflict", "check_failed", "requeued", "refused")
AFTER_STATES = ("ok", "failed", "timeout", "killed", "skipped")
STATS_S = 7 * 86400            # the conflict counts `ttp push --queue` and the web app show cover this long
_HEX40 = re.compile(r"[0-9a-f]{40}")
_children: dict[str, subprocess.Popen] = {}   # batch processes this daemon started, reaped by finalize
_default_branches: dict[tuple[str, str], tuple[float, str | None]] = {}   # (root, remote): (until, branch)
_recover_asked: dict[str, float] = {}         # batch id: when its remote was last asked (_recover)


# settings -----------------------------------------------------------------------------------------
def _delivery(p: Project, cfg: dict | None = None) -> dict:
    return (cfg if cfg is not None else p.config()).get("delivery") or {}


def _num(v: Any, default: float, cast: Callable, low: float):
    try:
        return max(low, cast(v)) if v is not None else default
    except (TypeError, ValueError):
        return default


def enabled(p: Project, cfg: dict | None = None) -> bool:
    """The queue is on, the project allows pushing and has a push branch: project.push_queue_on,
    which also picks the review prompt's push section, so the daemon and the reviews agree."""
    return push_queue_on({"delivery": _delivery(p, cfg)})


def target(p: Project) -> tuple[str, str] | None:
    """(remote, branch) of `delivery.push_branch`, or None when it is not set."""
    try:
        return push.target(p, p.root)
    except ValueError:
        return None


def _git(p: Project, *args: str, timeout: float = 120, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(p.root), *args], text=True, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **kw)


def _default_branch(p: Project, remote: str, now: float | None = None) -> str | None:
    """The remote's default branch, None when unknown. One `git ls-remote` (up to REFUSAL_TIMEOUT_S)
    per remote and DEFAULT_BRANCH_TTL_S (DEFAULT_BRANCH_RETRY_S while it fails): many approvals in
    one tick must not each wait on the network."""
    now = time.time() if now is None else now
    key = (str(p.root), remote)
    hit = _default_branches.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        ls = _git(p, "ls-remote", "--symref", remote, "HEAD", timeout=REFUSAL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        ls = None
    if ls is None or ls.returncode != 0:
        _default_branches[key] = (now + DEFAULT_BRANCH_RETRY_S, None)
        return None
    found = ""
    for line in ls.stdout.splitlines():
        ref = line[5:].split("\t")[0] if line.startswith("ref: ") else ""
        if ref.startswith("refs/heads/"):
            found = ref[len("refs/heads/"):]
            break
    _default_branches[key] = (now + DEFAULT_BRANCH_TTL_S, found)
    return found


def _refusal(p: Project, remote: str, branch: str, allow: bool = False) -> str:
    """push.refusal for the daemon: the same rules, bounded in time. An unreachable remote does not
    refuse here: the batch asks again before it pushes, and a network blip must not fail a review.
    `allow` (push.allow_protected) lets main, master and the remote's default branch through."""
    if branch in push.PROTECTED and not (allow and branch != "HEAD"):
        return f"refusing to push to {remote}/{branch}" + (f": {push.PROTECTED_HINT}" if branch != "HEAD" else "")
    if not allow and _default_branch(p, remote) == branch:
        return f"refusing to push to {remote}/{branch}, the remote's default branch: {push.PROTECTED_HINT}"
    return ""


def _state(db) -> dict:
    v = db.kv(KV) or {}
    return v if isinstance(v, dict) else {}


def _event(db, task: dict | None, kind: str, text: str, queued: bool, severity: str = "normal") -> None:
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
         (time.time(), f"task:{task['id']}" if task else "daemon", kind, severity, text[:6000],
          "queued" if queued else "handled", task["id"] if task else None))


def _reply(db, task: dict, new: str, text: str, need: str | None = None) -> None:
    """The answer to the chat that asked for the task, as Daemon._finish_worker gives it."""
    if not task["reply_chat"] or new not in ("done", "failed", "blocked"):
        return
    text = text if new == "done" else f"(task #{task['id']} {new}) {text}"
    if need and need not in text:
        text += f"\nNeeds from you: {need}"
    db.post("out", text[:6000], chat=None if task["reply_chat"] == "all" else task["reply_chat"],
            kind="reply", severity="normal")


def _short(sha: str | None) -> str:
    return (sha or "?")[:7]


def _reach(db, batch: str | None, b: dict | None = None, m: dict | None = None) -> dict | None:
    """push.reach as batch `batch` recorded it in its marker (`m` when `b` is that batch): whether
    what it pushed is on the branch the work is meant to reach. None when unknown or not asked."""
    if b is not None and m is not None and batch == b["id"]:
        r = m.get("reach")
    else:
        row = db.one("SELECT marker FROM push_batches WHERE id=?", (batch,)) if batch else None
        r = _read(Path(row["marker"])).get("reach") if row else None
    return r if isinstance(r, dict) else None


def _landing(row: dict, target: str, reach: dict | None) -> str:
    """A pushed or landed row in words: the exact branch and short sha, and whether it is on the
    branch it is meant to reach (push.reach_words), so "pushed" never reads as landed there."""
    if row["status"] == "pushed":
        return push.landing(row["pushed_sha"], target, reach, row["version"])
    return f"already on {target} at {_short(row['pushed_sha'])}" + push.reach_words(reach)


def target_line(db) -> str | None:
    """The daily review's line on how far the intended target (delivery.base_ref) is behind the push
    branch, from the reach the last pushed batch recorded in its marker: no git, no model. None when
    no batch recorded a reach (the branches are the same, no marker, or nothing pushed yet)."""
    row = db.one("SELECT id, target, ended, started FROM push_batches WHERE outcome IN ('pushed','landed') "
                 "ORDER BY started DESC LIMIT 1")
    r = _reach(db, row["id"]) if row else None
    if not r or not r.get("ref") or r["ref"] == row["target"]:
        return None
    when = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(row["ended"] or row["started"]))
    if r.get("on") is None:
        return f"Intended target {r['ref']} could not be read at the last push to {row['target']} ({when})"
    n = r.get("behind") if not r.get("on") else 0
    what = (f"is {n} commit{'s' if n != 1 else ''} behind" if isinstance(n, int) and n > 0
            else "is not yet up to date with" if not r.get("on") else "is up to date with")
    return f"Intended target {r['ref']} {what} {row['target']} (as of the last push, {when})"


# approval -----------------------------------------------------------------------------------------
def _commit(p: Project, ref: str) -> str:
    r = _git(p, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else ""


def _contains(p: Project, heads: list[str], commit: str) -> bool:
    """Whether `commit` is one of `heads` or an ancestor of one: one git call for all of them."""
    heads = [h for h in heads if h]
    if commit in heads:
        return True
    if not heads:
        return False
    r = _git(p, "rev-list", "-n1", commit, "--not", *heads)
    return r.returncode == 0 and not r.stdout.strip()


def check_approval(p: Project, task: dict, entries: Any, cfg: dict | None = None) -> dict:
    """Validate a review's `push` list with fast local git: {"ignored": why} in a project that does
    not push or, while the queue is off, for anything but a list of approvals; {"invalid": why} (also
    for such a list while the queue is off: turned off while the review ran, it runs again and
    pushes itself, as queue_off sends back approvals made before), or {"entries": [{"branch", "head"}], "target": "remote/branch", "added": {head:
    [commit lines]}}. Each head is a full hash of a commit in the project root that the review
    reviewed (worktree.reviewed_refs) or an ancestor of one. A re-approval after a push conflict may
    name any commit; the commits it adds over the conflicting head are listed for the event."""
    from .worktree import reviewed_refs
    d = _delivery(p, cfg)
    if d.get("push_queue") is not True:
        if isinstance(entries, list) and entries and all(isinstance(e, dict) and e.get("head") for e in entries):
            return {"invalid": "the push queue is off (delivery.push_queue): push with ttp push"}
        return {"ignored": "the push queue is off (delivery.push_queue)"}
    if not push_allowed(d):
        return {"ignored": "this project does not allow pushing (delivery.push_allowed)"}
    if push.REVIEW_ONLY_LABEL in push._labels(task):
        return {"ignored": "this review is review only: its change must not reach the push branch"}
    tgt = target(p) if enabled(p, cfg) else None
    if not tgt:
        return {"invalid": "no target branch: set delivery.push_branch"}
    why = _refusal(p, *tgt, push.allow_protected(d))
    if why:
        return {"invalid": why}
    if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
        return {"invalid": 'list the approved commits as "push": [{"branch": ..., "head": <40-hex sha>}]'}
    rows = p.db.q("SELECT status, head, created FROM push_queue WHERE task=? ORDER BY id", (task["id"],))
    conflicted = [r["head"] for r in rows if r["created"] == rows[-1]["created"] and r["status"] == "conflict"]
    reviewed: list[str] | None = None
    out, added = [], {}
    for e in entries:
        head = str(e.get("head") or "").strip().lower()
        if not _HEX40.fullmatch(head):
            return {"invalid": f"head {head[:60] or '(none)'!r} is not a full 40-character commit hash"}
        if not _commit(p, head):
            return {"invalid": f"{head} is not a commit in {p.root}"}
        if conflicted:
            log = _git(p, "log", "--format=%h %s", "-n", "20", head, "--not", *conflicted)
            added[head] = log.stdout.splitlines() if log.returncode == 0 else []
        else:
            if reviewed is None:
                refs = reviewed_refs(p, task)
                reviewed = [c for c in (_commit(p, r) for r in refs) if c]
                names = ", ".join(refs) or "none"
            if not _contains(p, reviewed, head):
                return {"invalid": f"{head} is none of the reviewed refs ({names}) nor an ancestor of one"}
        out.append({"branch": str(e.get("branch") or "").strip()[:200], "head": head})
    return {"entries": out, "target": f"{tgt[0]}/{tgt[1]}", "added": added}


def approve(p: Project, task_id: int, run_id: int | None, approval: dict) -> list[int]:
    """One `approved` row per entry, each pinned at refs/ttp/push/<id>. Call it inside the
    transaction that also moves the task to `pushing`: a failed pin rolls the rows back, and a pin
    without its row is removed by prune_refs."""
    now = time.time()
    ids = []
    for e in approval["entries"]:
        rid = p.db.x("INSERT INTO push_queue(task,run,branch,head,target,status,created,updated) "
                     "VALUES(?,?,?,?,?,'approved',?,?)",
                     (task_id, run_id, e["branch"], e["head"], approval["target"], now, now))
        pin = _git(p, "update-ref", f"{REF_PREFIX}{rid}", e["head"])
        if pin.returncode != 0:
            raise RuntimeError(f"could not pin {e['head']} at {REF_PREFIX}{rid}: {pin.stderr.strip()}")
        ids.append(rid)
    return ids


def queued_text(task: dict, approval: dict) -> str:
    """The feed line of an approval."""
    what = ", ".join(f"{e['branch'] or '?'} at {_short(e['head'])}" for e in approval["entries"])
    text = f"#{task['id']} {task['title']}: approved {what} for {approval['target']}; it goes out with the next batch"
    for head, lines in (approval.get("added") or {}).items():
        text += (f"\nRe-approved after a push conflict; {_short(head)} adds: "
                 + ("; ".join(lines[:20]) if lines else "no new commits"))
    return text


# edges --------------------------------------------------------------------------------------------
def _requeue(db, task: dict, woke: str) -> None:
    """Run the review again at once and tell its run why (prompts.worker_task shows `woke`)."""
    prev = load_result(task["result"])
    db.update_task(task["id"], status="queued", not_before=None, blocked_reason=None,
                   result=dump_result({**prev, "woke": woke[:1500]}))


def _cancel(db, ids: list[int]) -> None:
    if ids:
        db.x(f"UPDATE push_queue SET status='cancelled', updated=? WHERE status='approved' AND id IN "
             f"({','.join('?' * len(ids))})", (time.time(), *ids))


def cancel_orphans(p: Project) -> int:
    """Approved rows of a task that no longer waits for its push are cancelled. The coordinator, the
    web app or `ttp task cancel` moved it (cancelled or queued: expected; anything else is reported).
    Rows already in a batch cannot be withdrawn: the batch reports them."""
    db = p.db
    rows = db.q("SELECT q.id, q.task, t.status FROM push_queue q LEFT JOIN tasks t ON t.id=q.task "
                "WHERE q.status='approved' AND (t.status IS NULL OR t.status!='pushing')")
    if not rows:
        return 0
    with db.tx():
        by_task: dict[int, list[dict]] = {}
        for r in rows:
            by_task.setdefault(r["task"], []).append(r)
        for tid, rs in by_task.items():
            _cancel(db, [r["id"] for r in rs])
            task = db.task(tid)
            status = rs[0]["status"] or "gone"
            expected = status in ("cancelled", "queued", "running", "gone")
            _event(db, task, "push_cancelled",
                   f"#{tid}: {len(rs)} approved push{'es' if len(rs) > 1 else ''} withdrawn, since the task is "
                   f"{status}" + ("" if expected else "; requeue it to push again"), queued=not expected)
    return len(rows)


def queue_off(p: Project, cfg: dict | None = None) -> int:
    """With the queue turned off, reviews still waiting on it run again and push themselves."""
    if enabled(p, cfg):
        return 0
    db = p.db
    tasks = db.q("SELECT DISTINCT t.* FROM push_queue q JOIN tasks t ON t.id=q.task "
                 "WHERE q.status='approved' AND t.status='pushing'")
    if not tasks:
        return 0
    with db.tx():
        for t in tasks:
            _cancel(db, [r["id"] for r in db.q("SELECT id FROM push_queue WHERE task=? AND status='approved'",
                                                (t["id"],))])
            _requeue(db, t, "push queue turned off: push with ttp push")
            _event(db, t, "task_requeued", f"#{t['id']} {t['title']}: the push queue was turned off; its review "
                                           f"runs again and pushes with ttp push", queued=False)
    return len(tasks)


def retarget(p: Project, name: str) -> int:
    """Approvals for another target (delivery.push_branch changed) send their reviews back."""
    db = p.db
    rows = db.q("SELECT q.id, q.task, q.target FROM push_queue q JOIN tasks t ON t.id=q.task "
                "WHERE q.status='approved' AND t.status='pushing' AND q.target!=?", (name,))
    if not rows:
        return 0
    with db.tx():
        for tid in dict.fromkeys(r["task"] for r in rows):
            task = db.task(tid)
            old = next(r["target"] for r in rows if r["task"] == tid)
            _cancel(db, [r["id"] for r in db.q("SELECT id FROM push_queue WHERE task=? AND status='approved'",
                                                (tid,))])
            woke = f"push target changed: approved for {old}, the target is now {name}"
            _requeue(db, task, woke)
            _event(db, task, "task_requeued", f"#{tid} {task['title']}: {woke}; its review runs again", queued=False)
    return len(rows)


# scheduling ---------------------------------------------------------------------------------------
def _approved(db, name: str) -> list[dict]:
    return db.q("SELECT q.*, t.priority FROM push_queue q JOIN tasks t ON t.id=q.task WHERE q.status='approved' "
                "AND t.status='pushing' AND q.target=? ORDER BY t.priority, q.id", (name,))


def _tracking_tip(p: Project, remote: str, branch: str) -> str:
    """The target's tip as last fetched: no network in the tick."""
    return _commit(p, f"refs/remotes/{remote}/{branch}")


def _gap_left(db, now: float, gap: float) -> float:
    """Seconds until delivery.push_min_gap_s has passed since the last batch ended, from the end it
    recorded in push_batches (a dead batch's is when finalize found it), so a restart or a failed batch
    never stretches the wait. An end in the future (the clock went back) counts as passed."""
    if gap <= 0:
        return 0.0
    last = db.one("SELECT MAX(COALESCE(ended, finalized, started)) AS t FROM push_batches")
    t = float(last["t"]) if last and last["t"] is not None else None
    return 0.0 if t is None or t > now else max(0.0, t + gap - now)


def due(p: Project, now: float | None = None, cfg: dict | None = None) -> tuple[list[dict], str]:
    """The approved rows a batch should take now (at most delivery.push_batch_max, best priority
    first) and why; or ([], why not). Only local state is read: no fetch, no checks. Unless the batch
    is full or holds a priority-1 approval, it waits for delivery.push_min_gap_s since the last one."""
    now = time.time() if now is None else now
    if not enabled(p, cfg):
        return [], "the push queue is off"
    tgt = target(p)
    if not tgt:
        return [], "no push target (delivery.push_branch)"
    remote, branch = tgt
    db = p.db
    live = db.one("SELECT id FROM push_batches WHERE after_finalized IS NULL ORDER BY started LIMIT 1")
    if live:
        return [], f"batch {live['id']} is still going"
    rows = _approved(db, f"{remote}/{branch}")
    if not rows:
        return [], "nothing approved"
    paused = db.paused_resources()
    if push.lock_name(remote, branch) in paused or "push" in paused:
        return [], "the push resource is paused"
    st = _state(db)
    if float(st.get("backoff_until") or 0) > now:
        return [], f"backing off until {time.strftime('%H:%M', time.localtime(st['backoff_until']))}"
    if not locks.any_free(push.lock_paths(p, remote, branch)) or any(
            not locks.any_free([x]) for x in sorted((p.state / "locks").glob("push:run-*.lock"))):
        return [], "another push holds its turn"
    hold = st.get("hold") or {}
    if (hold and now < float(hold.get("until") or 0) and sorted(r["id"] for r in rows) == sorted(hold.get("rows") or [])
            and _tracking_tip(p, remote, branch) == hold.get("tip")):
        return [], f"held: the same approvals on the same tip {_short(hold.get('tip'))}, which failed its checks"
    d = _delivery(p, cfg)
    window = _num(d.get("push_batch_s"), DEFAULT_BATCH_S, float, 0)
    cap = int(_num(d.get("push_batch_max"), DEFAULT_BATCH_MAX, int, 1))
    gap = _num(d.get("push_min_gap_s"), DEFAULT_MIN_GAP_S, float, 0)
    if len(rows) >= cap:
        why = f"{len(rows)} approvals"
    elif any(r["priority"] == 1 for r in rows):
        why = "a priority-1 review"
    elif (left := _gap_left(db, now, gap)) > 0:
        return [], f"{left:.0f} s left of the {gap:.0f} s gap since the last batch ended"
    elif now - min(r["created"] for r in rows) >= window:
        why = f"the oldest approval waited {window:.0f} s"
    elif not db.one("SELECT id FROM tasks WHERE kind='review' AND status='running' LIMIT 1") and not any(
            t["kind"] == "review" for t in db.ready_tasks()):
        why = "no review is running or ready"
    else:
        return [], "waiting for more approvals"
    return rows[:cap], why


def batch_argv(marker: Path) -> list[str]:
    """The batch process (T1's `ttp push --batch`); tests put a stub here."""
    return [sys.executable, "-m", "ttp", "push", "--batch", str(marker)]


def _child_env(p: Project) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("TTP_RUN_DIR", "TTP_TASK", "TTP_RUN_ID")}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), TTP_PROJECT=str(p.base))
    return env


def _spawn(p: Project, bid: str, marker: Path, lock) -> subprocess.Popen:
    """Start the batch process on `marker`, holding `lock` from its first instant (inherited, as
    push.detach does), in a session of its own so a daemon restart leaves it running. It starts once
    its stdin closes: the go, given after the marker is complete. It runs niced like the workers
    (runner.nice), checks and after_push commands included."""
    env = _child_env(p)
    env["TTP_BATCH_LOCK_FD"] = str(lock.fileno())   # batch._inherited trusts only this descriptor
    with open(marker.with_suffix(".log"), "ab") as out:
        child = subprocess.Popen(batch_argv(marker), cwd=str(p.root), env=env, stdin=subprocess.PIPE,
                                 stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                 pass_fds=(lock.fileno(),))
    renice(child.pid, nice_level(p.config().get("runner"))[0])   # before the go
    _children[bid] = child
    return child


def start(p: Project, rows: list[dict], name: str, now: float | None = None) -> str | None:
    """Start a batch for `rows` (see due): marker, run lock, rows `batched`, a push_batches row,
    then the detached process. Returns the batch id, or None when nothing started."""
    now = time.time() if now is None else now
    db = p.db
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True, exist_ok=True)
    push._prune(folder, now)
    bid = base = "batch-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{os.getpid()}"
    n = 1
    while (folder / f"{bid}.json").exists() or db.one("SELECT id FROM push_batches WHERE id=?", (bid,)):
        n += 1
        bid = f"{base}-{n}"
    marker = folder / f"{bid}.json"
    lock_path = push._run_lock(marker)
    lock = locks.try_take([lock_path], f"push batch {bid}", "the push queue")
    if lock is None:
        return None
    try:
        m = {"v": 1, "kind": "batch", "id": bid, "status": "running", "phase": "push", "pid": None, "started": now,
             "repo": str(p.root), "target": name, "log": str(marker.with_suffix(".log")), "lock": str(lock_path),
             "entries": [{"id": r["id"], "task": r["task"], "branch": r["branch"], "head": r["head"],
                          "ref": f"{REF_PREFIX}{r['id']}"} for r in rows]}
        write_json(marker, m)
        ids = [r["id"] for r in rows]
        with db.tx():
            took = db.conn.execute(f"UPDATE push_queue SET status='batched', batch=?, updated=? WHERE status='approved' "
                                   f"AND id IN ({','.join('?' * len(ids))})", (bid, now, *ids)).rowcount
            if took != len(ids):
                raise _Changed()
            db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)", (bid, str(marker), name, now))
        # From here on finalize owns the batch: a process that never starts is a batch that died.
        child = _spawn(p, bid, marker, lock)
        m["pid"] = child.pid
        write_json(marker, m)
        child.stdin.close()      # the go
    except _Changed:
        marker.unlink(missing_ok=True)
        return None
    finally:
        lock.close()             # the child holds the run lock from here on
    return bid


class _Changed(Exception):
    """Rows left `approved` between due() and start(): nothing starts."""


def schedule(p: Project, cfg: dict | None = None, log: Callable[[str], None] = lambda m: None) -> str | None:
    """The tick's start step: send back approvals for another target, then start a batch if one is
    due. Cheap while nothing is approved: no git, no locks."""
    db = p.db
    if not enabled(p, cfg) or not db.one("SELECT id FROM push_queue WHERE status='approved' LIMIT 1"):
        return None
    tgt = target(p)
    if not tgt:
        return None
    name = f"{tgt[0]}/{tgt[1]}"
    if retarget(p, name):
        log(f"push queue: approvals for an old target sent back to their reviews (target now {name})")
    rows, why = due(p, cfg=cfg)
    if not rows:
        return None
    bid = start(p, rows, name)
    if bid:
        log(f"push queue: started {bid} with {len(rows)} approval{'s' if len(rows) > 1 else ''} ({why})")
    return bid


# finalize -----------------------------------------------------------------------------------------
def _read(marker: Path) -> dict:
    return push._read(marker)


def alive(marker: Path) -> tuple[bool, dict]:
    """(whether the batch process of `marker` runs, the marker as read after testing its lock), in
    the order push.result uses: read the marker, test the lock it names, read it again. A lock field
    that changed meanwhile (the batch moved to its after_push lock) is tested again. A batch is dead
    when it is not finished and its current lock is free; a missing marker is a dead batch."""
    m = _read(marker)
    for _ in range(3):
        lock = m.get("lock")
        free = locks.any_free([Path(lock)]) if lock else True
        again = _read(marker)
        if again.get("lock") == lock:
            return not free and again.get("phase") != "finished", again
        m = again
    return False, m


def _reap(bid: str) -> None:
    child = _children.get(bid)
    if child is not None and child.poll() is not None:
        _children.pop(bid, None)


def _detail(r: dict) -> dict:
    try:
        d = json.loads(r.get("detail") or "{}")
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def _tail_of(m: dict, detail: dict | None = None, n: int = 20) -> str:
    tail = str((detail or {}).get("tail") or "")
    return "\n".join((tail or push._tail(m.get("log"), n)).splitlines()[-n:])


def _died(p: Project, b: dict, m: dict, alert: Callable, why: str, now: float, queued: bool = False) -> None:
    """A batch that ended without a usable outcome: its rows go back to approved, a try counted, and
    the queue backs off. DYING_AFTER of them in a row raise push_queue_dying. `queued`: the event
    needs the coordinator (something is left that the queue cannot recover)."""
    db = p.db
    with db.tx():
        if not db.conn.execute("UPDATE push_batches SET outcome=?, ended=?, finalized=?, after_push='skipped', "
                               "after_finalized=? WHERE id=? AND finalized IS NULL",
                               ("error" if m.get("outcome") == "error" else "died", now, now, now, b["id"])).rowcount:
            return
        rows = db.q("SELECT id, tries FROM push_queue WHERE batch=? AND status='batched'", (b["id"],))
        db.x("UPDATE push_queue SET status='approved', tries=tries+1, updated=? WHERE batch=? AND status='batched'",
             (now, b["id"]))
        tries = max([r["tries"] + 1 for r in rows] or [1])
        st = _state(db)
        st["deaths"] = int(st.get("deaths") or 0) + 1
        st["backoff_until"] = now + min(BACKOFF_MAX_S, BACKOFF_STEP_S * tries)
        db.set_kv(KV, st)
        _event(db, None, "push_batch_died",
               f"push batch {b['id']} {why}; {len(rows)} approval{'s' if len(rows) != 1 else ''} back in the queue, "
               f"retried after {st['backoff_until'] - now:.0f} s", queued=queued, severity="high" if queued else "normal")
    push._forget_lock(Path(b["marker"]))     # dead: nothing holds it; the next batch has its own
    if st["deaths"] >= DYING_AFTER:
        alert("push_queue_dying", f"The last {st['deaths']} push batches ended without finishing ({why}). The "
                                  f"approvals stay queued and are retried with a growing pause. Log: {m.get('log') or '?'}",
              severity="high")


def _on_remote(repo: Path, remote: str, branch: str, sha: str, fetch: bool) -> bool | None:
    """Whether `sha` is on remote/branch: the tracking ref first (a push that went through moved it),
    then a bounded fetch when asked. None when the branch cannot be read."""
    from .landed import on_branch
    if fetch:
        try:
            if subprocess.run(["git", "-C", str(repo), "fetch", "-q", remote,
                               f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=RECOVER_FETCH_S,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).returncode != 0:
                return None
        except (OSError, subprocess.TimeoutExpired):
            return None
    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                          f"refs/remotes/{remote}/{branch}^{{commit}}"], text=True, capture_output=True,
                         stdin=subprocess.DEVNULL).stdout.strip()
    if not tip:
        return None
    try:
        return on_branch(repo, sha, tip)
    except (OSError, subprocess.SubprocessError):
        return None


def _recover(p: Project, b: dict, marker: Path, m: dict, now: float) -> dict | None:
    """A batch that died with no outcome but had begun `git push` (batch.Batch._mark_pushing). If
    that head is on the branch, the push went through: the marker gets the outcome it would have
    written (pushed, with each entry's result), so finalize applies it and resumes its after_push,
    and no entry goes back to be pushed again. If it is not, the batch died as usual. While the
    branch cannot be read: None (asked again later), until RECOVER_S from the first such look (not from
    the push: a long outage must not use the window up; kept in the marker as pushing.first_checked,
    so a restart keeps it), then the batch died with `unverified` set."""
    pg = m["pushing"]
    sha = str(pg.get("sha") or "")
    remote, _, branch = str(b["target"] or "").partition("/")
    if not (_HEX40.fullmatch(sha) and remote and branch):
        return m
    repo = Path(m.get("repo") or p.root)
    on = _on_remote(repo, remote, branch, sha, fetch=False) or None   # not on the tracking ref: ask the remote
    if on is None and now - _recover_asked.get(b["id"], 0) >= RECOVER_GAP_S:
        _recover_asked[b["id"]] = now
        on = _on_remote(repo, remote, branch, sha, fetch=True)
    if on is None:
        try:
            since = float(pg["first_checked"])
        except (KeyError, TypeError, ValueError):
            since = now                                 # counted from the first look, kept across restarts
            fresh = dict(push._read(marker) or m)
            fresh["pushing"] = {**(fresh.get("pushing") if isinstance(fresh.get("pushing"), dict) else pg),
                                "first_checked": now}
            write_json(marker, fresh)
        if now - since < RECOVER_S:
            return None
        _recover_asked.pop(b["id"], None)
        return {**m, "unverified": sha}
    _recover_asked.pop(b["id"], None)
    if not on:
        return m
    also = push.fast_forward_list(_delivery(p).get("fast_forward_also"))
    m = dict(push._read(marker) or m)
    m.update(outcome="pushed", pushed_sha=sha, tip=pg.get("tip"), version=pg.get("version"),
             results=pg.get("results") or [], checks=pg.get("checks") or {}, rounds=pg.get("rounds"),
             message=None, recovered=now)
    if also:
        m["fast_forward"] = [f"not ff {x}: the batch stopped after its push, before this step (a reboot?); "
                             f"not run" for x in also]
    write_json(marker, m)
    _event(p.db, None, "push_batch_recovered", f"push batch {b['id']} stopped after pushing {_short(sha)} to "
                                               f"{b['target']}, before it wrote its outcome (a reboot?); it is on the "
                                               f"branch, so the batch counts as pushed", queued=False)
    return m


def _settle(p: Project, tid: int, b: dict, m: dict, now: float) -> None:
    """Move a review on from the rows of its latest approval, inside finalize's transaction."""
    db = p.db
    task = db.task(tid)
    rows = db.q("SELECT * FROM push_queue WHERE task=? ORDER BY id", (tid,))
    if not task or not rows:
        return
    here = [r for r in rows if r["batch"] == b["id"]]
    if task["status"] != "pushing":
        # Cancelled or requeued while its rows were in the batch: report what became of them.
        landed = [r for r in here if r["status"] in ("pushed", "landed")]
        _event(db, task, "push_reported",
               f"#{tid} {task['title']} ({task['status']}): batch {b['id']} " + "; ".join(
                   f"{r['branch'] or '?'} at {_short(r['head'])}: {r['status']}" for r in here),
               queued=bool(landed))
        return
    current = [r for r in rows if r["created"] == rows[-1]["created"]]   # one approval's rows share it
    states = {r["status"] for r in current}
    prev = load_result(task["result"])
    summary = str(prev.get("summary") or "")
    target = b["target"]
    if "check_failed" in states:
        r = next(r for r in current if r["status"] == "check_failed")
        d = _detail(r)
        cmd, tail = str(d.get("cmd") or "the push checks"), _tail_of(m, d)
        text = (f"the push checks failed on {r['branch'] or '?'} at {_short(r['head'])} in batch {b['id']}: "
                f"`{cmd}`\n{tail}")
        db.update_task(tid, status="failed", blocked_reason=None, result=dump_result(
            {**prev, "status": "failed", "summary": f"{text}\n(review: {summary})"[:1500],
             "push_failed": {"branch": r["branch"], "head": r["head"], "cmd": cmd, "tail": tail[-1500:],
                             "batch": b["id"]}}))
        _event(db, task, "task_failed", f"#{tid} {task['title']} → failed: {text}", queued=True)
        _reply(db, task, "failed", text)
    elif "refused" in states:
        r = next(r for r in current if r["status"] == "refused")
        d = _detail(r)
        why = str(d.get("why") or d.get("message") or _tail_of(m, d, 5) or "the batch refused it")
        reason = f"push refused: {why}"[:500]
        db.update_task(tid, status="blocked", blocked_reason=reason,
                       result=dump_result({**prev, "status": "blocked", "summary": summary}))
        _event(db, task, "task_blocked", f"#{tid} {task['title']} → blocked: {reason}", queued=True, severity="high")
        _reply(db, task, "blocked", summary, need=reason)
    elif "conflict" in states:
        r = next(r for r in current if r["status"] == "conflict")
        d = _detail(r)
        files = ", ".join(str(f) for f in (d.get("files") or [])[:20]) or "?"
        woke = (f"push conflict: {r['branch'] or '?'} at {r['head']} conflicts with {target} at "
                f"{d.get('onto') or m.get('tip') or '?'} in {files}")
        if d.get("cmd"):
            tail = _tail_of({}, d, 5)
            woke += f"; its checks failed after the batch merged changes of different lines: {d['cmd']}"
            if tail:
                woke += f" ({tail[-300:]})"
        done = [x for x in current if x["status"] in ("pushed", "landed")]
        if done:
            woke += "; already landed: " + ", ".join(f"{x['branch'] or '?'} at {_short(x['head'])} ({x['status']} as "
                                                     f"{_short(x['pushed_sha'])})" for x in done)
        if int(task["attempts"] or 0) >= int(task["max_attempts"] or 3):
            text = f"{woke}. It keeps conflicting after {task['attempts']} attempts"
            db.update_task(tid, status="failed", blocked_reason=None,
                           result=dump_result({**prev, "status": "failed", "summary": text[:1500]}))
            _event(db, task, "task_failed", f"#{tid} {task['title']} → failed: {text}", queued=True)
            _reply(db, task, "failed", text)
        else:
            _requeue(db, task, woke)
            _event(db, task, "task_requeued", f"#{tid} {task['title']}: {woke}; its review runs again to settle it",
                   queued=False)
    elif states <= {"pushed", "landed"}:
        done = [x for x in rows if x["status"] in ("pushed", "landed")]
        last = max(done, key=lambda x: x["id"])
        new = [x for x in current if x["status"] == "pushed"]
        tail = _landing({**last, "status": "pushed" if new else "landed"}, last["target"] or target,
                        _reach(db, last["batch"], b, m))
        ff = [str(x) for x in m.get("fast_forward") or []] if new else []
        if ff:   # delivery.fast_forward_also, as read back from the remote
            tail += "; " + "; ".join(f"warning: {x}" if x in push.ff_warnings([x]) else x for x in ff)
        result = {**prev, "status": "done", "summary": f"{summary.rstrip()} ({tail})".strip()[:1500],
                  "pushed": [{"branch": x["branch"], "head": x["head"], "sha": x["pushed_sha"], "version": x["version"],
                              "batch": x["batch"], "status": x["status"]} for x in done]}
        if ff:
            result["fast_forward"] = ff
        result.pop("woke", None)
        db.update_task(tid, status="done", blocked_reason=None, result=dump_result(result))
        # Handled: the coordinator sees it in its next turn's task list; no turn is spent on it.
        _event(db, task, "task_done", f"#{tid} {task['title']} → done ({tail}, batch {b['id']}): {summary[:1200]}",
               queued=False)
        _reply(db, task, "done", result["summary"])
        settle_reviewed(db, now, only=_covers(db, task, rows))
    # else: some of its rows are still approved or batched; it keeps waiting.


# reviewed code tasks ------------------------------------------------------------------------------
def _covers(db, review: dict, rows: list[dict], code: list[dict] | None = None) -> set[int]:
    """The tasks whose work a review's push ships: its parent and dependencies, the task its title or
    label names, the code tasks whose branch it pushed or (naming no subject) its spec names, and the
    tasks each of those continues (a fix carries the change it fixes)."""
    ids = {d for d in dependency_ids(review) if isinstance(d, int)}
    ids |= {i for i in (review.get("parent"), review_subject(review)) if isinstance(i, int)}
    branches = {r["branch"] for r in rows if r["branch"]}
    if code is None:
        code = db.q("SELECT id, branch, labels FROM tasks WHERE kind='code' AND branch IS NOT NULL AND branch!=''")
    ids |= {t["id"] for t in code if t["branch"] in branches or reviews_task(review, t)}
    out: set[int] = set()
    while ids:
        i = ids.pop()
        if i not in out:
            out.add(i)
            t = db.one("SELECT labels FROM tasks WHERE id=? AND kind='code'", (i,))
            if t and (c := continues_id(t)) is not None:
                ids.add(c)
    return out


def settle_reviewed(db, now: float | None = None, only: set[int] | None = None) -> list[int]:
    """Close the code tasks left in 'review' whose work shipped: the latest review covering one (see
    _covers) is done, and the push queue pushed or landed its approval since the task entered review.
    A review that settles a push conflict on a branch of its own ships the task as well. `only`
    limits it to those task ids. Returns the ids closed. No git, no model."""
    now = time.time() if now is None else now
    todo = [t for t in db.q("SELECT * FROM tasks WHERE kind='code' AND status='review' ORDER BY id")
            if only is None or t["id"] in only]
    if not todo:
        return []
    since = db.review_since()
    code = db.q("SELECT id, branch, labels FROM tasks WHERE kind='code' AND branch IS NOT NULL AND branch!=''")
    reviews = db.q("SELECT * FROM tasks WHERE kind='review' AND status!='cancelled' AND id>? ORDER BY id DESC",
                   (min(t["id"] for t in todo),))
    rows: dict[int, list[dict]] = {}
    for r in db.q("SELECT * FROM push_queue WHERE task IN (%s) ORDER BY id" % ",".join("?" * len(reviews)),
                  [r["id"] for r in reviews]) if reviews else []:
        rows.setdefault(r["task"], []).append(r)
    covers: dict[int, set[int]] = {}
    closed = []
    for t in todo:
        latest = None
        for r in reviews:
            if r["id"] not in covers:
                covers[r["id"]] = _covers(db, r, rows.get(r["id"], []), code)
            if t["id"] in covers[r["id"]]:
                latest = r
                break
        if not latest or latest["status"] != "done":
            continue
        shipped = [x for x in rows.get(latest["id"], []) if x["status"] in ("pushed", "landed")
                   and float(x["updated"] or 0) >= float(since.get(t["id"]) or 0)]
        if not shipped:
            continue
        last = max(shipped, key=lambda x: x["id"])
        tail = f"shipped by review #{latest['id']}: " + _landing(last, last["target"], _reach(db, last["batch"]))
        prev = load_result(t["result"])
        summary = str(prev.get("summary") or "")
        result = {**prev, "status": "done", "summary": f"{summary.rstrip()} ({tail})".strip()[:1500],
                  "shipped_by": latest["id"],
                  "pushed": [{"branch": x["branch"], "head": x["head"], "sha": x["pushed_sha"], "version": x["version"],
                              "batch": x["batch"], "status": x["status"]} for x in shipped]}
        with db.tx():
            if not db.conn.execute("UPDATE tasks SET status='done', blocked_reason=NULL, result=?, updated=? "
                                   "WHERE id=? AND status='review'", (dump_result(result), now, t["id"])).rowcount:
                continue
            _event(db, t, "task_done", f"#{t['id']} {t['title']} → done ({tail})", queued=False)
            _reply(db, t, "done", result["summary"])
        closed.append(t["id"])
    return closed


def _apply(p: Project, b: dict, m: dict, alert: Callable, now: float) -> None:
    """Apply a batch's outcome to its rows and their reviews in one transaction that also stamps the
    batch finalized; a batch already finalized is left alone."""
    db = p.db
    outcome = str(m.get("outcome"))
    results = {}
    for r in m.get("results") or []:
        if isinstance(r, dict) and str(r.get("id", "")).isdigit():
            results[int(r["id"])] = r
    sha, version, tip = m.get("pushed_sha"), m.get("version"), m.get("tip")
    checks = m.get("checks") if isinstance(m.get("checks"), dict) else {}
    later: list[tuple] = []
    with db.tx():
        if not db.conn.execute(
                "UPDATE push_batches SET outcome=?, ended=?, pushed_sha=?, version=?, tip=?, check_runs=?, check_s=?, "
                "finalized=? WHERE id=? AND finalized IS NULL",
                (outcome, m.get("ended") or now, sha, version, tip, checks.get("runs"), checks.get("seconds"), now,
                 b["id"])).rowcount:
            return
        rows = db.q("SELECT * FROM push_queue WHERE batch=? AND status='batched' ORDER BY id", (b["id"],))
        for r in rows:
            res = results.get(r["id"]) or {}
            status = res.get("status") if res.get("status") in ROW_RESULTS else (
                "refused" if outcome == "refused" else "requeued")
            detail = res.get("detail") if isinstance(res.get("detail"), dict) else None
            if outcome == "refused" and not detail:
                detail = {"why": str(m.get("message") or "")[:2000] or None}
            text = json.dumps(detail)[:8000] if detail else None
            if status in ("pushed", "landed"):
                own = res.get("sha") if isinstance(res.get("sha"), str) and _HEX40.fullmatch(res["sha"]) else None
                db.x("UPDATE push_queue SET status=?, pushed_sha=?, landed_sha=?, version=?, detail=?, updated=? "
                     "WHERE id=?", (status, sha or tip, own or sha or tip, version, text, now, r["id"]))
            elif status in ("conflict", "check_failed", "refused"):
                db.x("UPDATE push_queue SET status=?, detail=?, updated=? WHERE id=?", (status, text, now, r["id"]))
            else:   # requeued, or the batch did not get to it (busy, moved, rejected, tip_failed)
                db.x("UPDATE push_queue SET status='approved', tries=tries+?, updated=? WHERE id=?",
                     (1 if outcome == "moved" else 0, now, r["id"]))
        st = _state(db)
        st["deaths"] = 0
        back = [r["id"] for r in db.q("SELECT id FROM push_queue WHERE batch=? AND status='approved'", (b["id"],))]
        if outcome == "tip_failed" and back:
            st["hold"] = {"tip": tip, "rows": sorted(back), "until": now + HOLD_S}
        elif outcome in ("pushed", "landed", "nothing"):
            st.pop("hold", None)
            st.pop("backoff_until", None)
        if outcome == "tip_failed" and tip and tip not in (st.get("tips_told") or []):
            st["tips_told"] = ((st.get("tips_told") or []) + [tip])[-TIPS_TOLD:]
            d = m.get("tip_check") if isinstance(m.get("tip_check"), dict) else {}
            first = next((x.get("detail") for x in results.values() if isinstance(x.get("detail"), dict)
                          and x["detail"].get("cmd")), {}) or {}
            cmd = d.get("cmd") or first.get("cmd") or "the push checks"
            _event(db, None, "push_tip_failed", f"the push branch tip {_short(tip)} of {b['target']} fails its checks: "
                                                f"`{cmd}`\n{_tail_of(m, d or first)}", queued=True, severity="high")
        if warn := push.ff_warnings(m.get("fast_forward")):
            _event(db, None, "push_not_ff", f"push batch {b['id']} pushed {_short(sha)} to {b['target']}, but "
                                            f"delivery.fast_forward_also did not move every branch: "
                                            + "; ".join(warn), queued=True)
        if outcome == "rejected":
            st["backoff_until"] = now + BACKOFF_MAX_S
            later.append(("push_rejected", f"The push queue's push to {b['target']} was rejected: "
                                           f"{str(m.get('message') or _tail_of(m, None, 5))[:1500]}. "
                                           f"The approvals stay queued and are retried in {BACKOFF_MAX_S // 60} min.", "high"))
        db.set_kv(KV, st)
        for tid in dict.fromkeys(r["task"] for r in rows):
            _settle(p, tid, b, m, now)
        if outcome in ("moved", "busy", "rejected", "nothing", "landed", "pushed", "tip_failed") and back:
            _event(db, None, "push_requeued", f"push batch {b['id']} ended {outcome}: {len(back)} approval"
                                              f"{'s' if len(back) != 1 else ''} back in the queue", queued=False)
    for key, text, sev in later:
        alert(key, text, severity=sev)


def _after(p: Project, b: dict, m: dict, status: str, alert: Callable, now: float) -> None:
    """Record how the after_push step (the deploy) ended. Reviews stay done whatever it did."""
    db = p.db
    ap = m.get("after_push") if isinstance(m.get("after_push"), dict) else {}
    with db.tx():
        if not db.conn.execute("UPDATE push_batches SET after_push=?, after_finalized=? WHERE id=? AND "
                               "after_finalized IS NULL", (status, now, b["id"])).rowcount:
            return
        what = f"{_short(m.get('pushed_sha'))}" + (f" as {m['version']}" if m.get("version") else "")
        if status == "ok":
            took = (float(ap.get("ended") or 0) - float(ap.get("started") or 0)) if ap.get("ended") else None
            _event(db, None, "after_push_ok", f"after_push of {what} (batch {b['id']}) succeeded"
                                              + (f" in {took:.0f} s" if took and took > 0 else ""), queued=False)
        elif status in ("failed", "timeout", "killed"):
            tail = _tail_of(m, ap)
            how = {"failed": f"failed (exit {ap.get('exit')})", "timeout": "timed out",
                   "killed": "stopped before it finished, also when run once more (host reboots?)"}[status]
            if ap.get("cmd") and status != "killed":
                how += f" in `{ap['cmd']}`"
            text = f"after_push of {what} (batch {b['id']}) {how}. The reviews stay done.\n{tail}"
            _event(db, None, "after_push_failed", text, queued=True, severity="high")
    if status in ("failed", "timeout", "killed"):
        alert("after_push_failed", f"after_push of {what} (batch {b['id']}) {how}. Log: {m.get('log') or '?'}",
              severity="high")


def _after_push_set(p: Project, cfg: dict | None) -> bool:
    v = _delivery(p, cfg).get("after_push")
    if v is None or str(v).strip().lower() in ("", "none"):
        return False
    try:
        return bool(push.check_list(v))
    except (TypeError, ValueError):
        return True


def _resume(p: Project, b: dict, marker: Path, now: float) -> bool:
    """Run the after_push of a batch whose process died in it once more: the same
    `ttp push --batch <marker>`, which resumes at after_push since the marker has an outcome. It holds
    the batch's after_push lock (not `push:`, so upgrades do not wait for it), passed down as at the
    start, and the marker names it."""
    from .batch import _after_lock
    db = p.db
    lock_path = _after_lock(p, b["id"])
    lock = locks.try_take([lock_path], f"push batch {b['id']} after_push (resumed)", "the push queue")
    if lock is None:
        return False
    try:
        if not db.conn.execute("UPDATE push_batches SET after_tries=after_tries+1 WHERE id=? AND after_tries<? AND "
                               "after_finalized IS NULL", (b["id"], MAX_RESUMES)).rowcount:
            return False
        m = _read(marker)
        m.update(lock=str(lock_path), phase="after_push", resumed=now)
        write_json(marker, m)
        child = _spawn(p, b["id"], marker, lock)
        child.stdin.close()
        _event(db, None, "after_push_resumed", f"push batch {b['id']}: its after_push stopped before it finished "
                                               f"(a reboot?); running it once more", queued=False)
    finally:
        lock.close()
    return True


def _note_after_push(p: Project, cfg: dict | None) -> None:
    """Keep AFTER_PUSH_OFF current (written only when it changes): an after_push_failed alert also
    clears once after_push is unset or the queue is off, since no after_push will run to succeed."""
    off = not (enabled(p, cfg) and _after_push_set(p, cfg))
    if bool(p.db.kv(AFTER_PUSH_OFF)) != off:
        p.db.set_kv(AFTER_PUSH_OFF, off)


def finalize(p: Project, cfg: dict | None = None, alert: Callable = lambda *a, **k: None,
             now: float | None = None) -> list[str]:
    """The tick's finalize step (also while paused), idempotent: for each batch not yet finalized,
    read its marker and apply what it says. Returns the ids of the batches it changed."""
    db = p.db
    changed = []
    for b in db.q("SELECT * FROM push_batches WHERE after_finalized IS NULL ORDER BY started"):
        now_ = time.time() if now is None else now
        _reap(b["id"])
        marker = Path(b["marker"])
        live, m = alive(marker)
        if b["finalized"] is None:
            if not live and not m.get("outcome") and isinstance(m.get("pushing"), dict):
                m = _recover(p, b, marker, m, now_)
                if m is None:
                    continue
            if m.get("outcome") and m.get("outcome") != "error":
                _apply(p, b, m, alert, now_)
            elif live:
                continue
            else:
                why = (f"failed: {str(m.get('message') or '')[:300]}" if m.get("outcome") == "error"
                       else f"ended while pushing {_short(m['unverified'])} (a reboot, a kill or a crash), and "
                            f"{b['target']} could not be read for {RECOVER_S // 60} min to tell whether it landed. "
                            f"If it did, the next batch finds the approvals on the branch, but no after_push "
                            f"ran for it" if m.get("unverified")
                       else "ended before writing an outcome (a reboot, a kill or a crash)" if m
                       else "lost its marker")
                _died(p, b, m, alert, why, now_, queued=bool(m.get("unverified")))
                changed.append(b["id"])
                continue
            changed.append(b["id"])
            b = db.one("SELECT * FROM push_batches WHERE id=?", (b["id"],))
            if b["after_finalized"] is not None:
                continue
        ap = m.get("after_push") if isinstance(m.get("after_push"), dict) else {}
        status = ap.get("status") if ap.get("status") in AFTER_STATES else None
        if m.get("phase") == "finished" or (status and not live):
            _after(p, b, m, status or "skipped", alert, now_)
        elif live:
            continue
        elif m.get("outcome") != "pushed" or not _after_push_set(p, cfg):
            _after(p, b, m, "skipped", alert, now_)   # nothing to deploy
        elif int(b["after_tries"] or 0) < MAX_RESUMES:
            if _resume(p, b, marker, now_):
                changed.append(b["id"])
            continue
        else:
            _after(p, b, {**m, "after_push": {**ap, "status": "killed"}}, "killed", alert, now_)
        if b["id"] not in changed:
            changed.append(b["id"])
    return changed


def prune_refs(p: Project) -> int:
    """Delete the pin of each row that is pushed, landed, cancelled or otherwise settled once its
    task is finished, and pins without a row (an approval that rolled back). A conflict, check
    failure or refusal keeps its pin while its task is open."""
    db = p.db
    if not db.one("SELECT id FROM push_queue LIMIT 1"):
        return 0
    refs = _git(p, "for-each-ref", "--format=%(refname)", REF_PREFIX)
    gone = 0
    for ref in refs.stdout.split() if refs.returncode == 0 else []:
        tail = ref[len(REF_PREFIX):]
        row = db.one("SELECT q.status, t.status AS task_status FROM push_queue q LEFT JOIN tasks t ON t.id=q.task "
                     "WHERE q.id=?", (int(tail),)) if tail.isdigit() else None
        if row and (row["status"] in ("approved", "batched") or row["task_status"] not in (*TERMINAL_TASK_STATES, None)):
            continue
        if _git(p, "update-ref", "-d", ref).returncode == 0:
            gone += 1
    return gone


def tend(p: Project, cfg: dict | None = None, alert: Callable = lambda *a, **k: None,
         may_requeue: bool = True) -> list[str]:
    """The tick's first-loop step: withdraw approvals whose review moved on, send reviews back when
    the queue was turned off (not while the settings cannot be read: `may_requeue` False), and
    finalize batches. Cheap while the queue is empty."""
    for bid in list(_children):
        _reap(bid)
    db = p.db
    if may_requeue:
        _note_after_push(p, cfg)
    if not db.one("SELECT id FROM push_queue WHERE status IN ('approved','batched') LIMIT 1") and not db.one(
            "SELECT id FROM push_batches WHERE after_finalized IS NULL LIMIT 1"):
        return []
    cancel_orphans(p)
    if may_requeue:
        queue_off(p, cfg)
    return finalize(p, cfg, alert)


# reading ------------------------------------------------------------------------------------------
def pushed_heads(db, since: float = 0) -> set[str]:
    """Heads the queue pushed or landed (approved after `since`): work that is on the push branch."""
    return {r["head"] for r in db.q("SELECT head FROM push_queue WHERE status IN ('pushed','landed') AND created>=?",
                                    (since,))}


def conflict_stats(db, since: float) -> dict:
    """How the queue's entries that a batch settled since `since` fared with conflicts: entries,
    those that conflicted, those the batch resolved itself (their detail names what it "settled"),
    those sent back for a new rebase and review, and both rates in whole percents."""
    rows = db.q("SELECT status, detail FROM push_queue WHERE updated>=? AND status IN "
                "('pushed','landed','conflict','check_failed','refused')", (since,))
    auto = back = 0
    for r in rows:
        try:
            d = json.loads(r["detail"]) if r["detail"] else {}
        except ValueError:
            d = {}
        d = d if isinstance(d, dict) else {}
        files = d.get("files") or []
        if r["status"] == "conflict" and files and str(files[0]).startswith("(the rebase"):
            continue  # the rebase stopped or did not finish: not a conflict, as in Batch.conflicts()
        auto += bool(d.get("settled") and r["status"] != "conflict")   # sent back: not resolved
        back += r["status"] == "conflict"
    n = len(rows)
    return {"entries": n, "conflicted": back + auto, "auto_resolved": auto, "sent_back": back,
            "conflict_pct": round(100 * (back + auto) / n) if n else 0, "sent_back_pct": round(100 * back / n) if n else 0}


def summary(p: Project, now: float | None = None, last: int = 5, db=None) -> dict:
    """The queue for status displays: approved rows with their ages, the live batch's phase and
    age, and the last batches with outcome, sha, version and after_push. `db` is the caller's
    connection (the web app's threads each have their own); default p.db."""
    now = time.time() if now is None else now
    db = db or p.db
    approved = [{"id": r["id"], "task": r["task"], "branch": r["branch"], "head": r["head"], "target": r["target"],
                 "tries": r["tries"], "age_s": round(now - r["created"], 1)}
                for r in db.q("SELECT * FROM push_queue WHERE status='approved' ORDER BY id")]
    live = None
    b = db.one("SELECT * FROM push_batches WHERE after_finalized IS NULL ORDER BY started DESC LIMIT 1")
    if b:
        m = _read(Path(b["marker"]))
        live = {"id": b["id"], "target": b["target"], "age_s": round(now - b["started"], 1),
                "phase": m.get("phase") or ("after_push" if b["finalized"] else "push"),
                "rows": db.one("SELECT COUNT(*) n FROM push_queue WHERE batch=?", (b["id"],))["n"]}
    batches = [{k: r[k] for k in ("id", "target", "outcome", "pushed_sha", "version", "after_push", "started", "ended",
                                  "check_runs", "check_s")}
               for r in db.q("SELECT * FROM push_batches WHERE finalized IS NOT NULL ORDER BY started DESC LIMIT ?",
                             (last,))]
    for x in batches:
        x["reach"] = _reach(db, x["id"]) if x["outcome"] in ("pushed", "landed") else None
        x["landing"] = (push.landing(x["pushed_sha"], x["target"], x["reach"], x["version"]) if x["pushed_sha"] and
                        x["outcome"] == "pushed" else f"already on {x['target']}{push.reach_words(x['reach'])}"
                        if x["outcome"] == "landed" else None)
    pushed = db.one("SELECT id, target, pushed_sha, version, ended, after_push FROM push_batches WHERE outcome='pushed' "
                    "AND pushed_sha IS NOT NULL ORDER BY started DESC LIMIT 1")
    known = {x["id"]: x["reach"] for x in batches}
    if pushed:
        pushed["reach"] = known[pushed["id"]] if pushed["id"] in known else _reach(db, pushed["id"])
    st = _state(db)
    return {"on": enabled(p), "approved": approved, "live": live, "last": batches, "last_pushed": pushed,
            "backoff_until": st.get("backoff_until"), "hold": st.get("hold"), "deaths": int(st.get("deaths") or 0),
            "conflicts": conflict_stats(db, now - STATS_S)}


# What a live batch is doing, a finished batch's outcome and its after_push, in words for the user.
PHASE_WORDS = {"push": "checking", "after_push": "deploying", "finished": "finishing"}
OUTCOME_WORDS = {"pushed": "pushed", "landed": "already on the branch", "nothing": "nothing to push",
                 "conflict": "conflicted", "tip_failed": "the branch tip fails its checks", "moved": "the branch kept moving",
                 "busy": "another push held the branch", "rejected": "rejected by the remote", "refused": "refused",
                 "died": "ended before finishing", "error": "failed"}
AFTER_WORDS = {"ok": "deploy ok", "failed": "deploy failed", "timeout": "deploy timed out",
               "killed": "deploy cut short", "skipped": "no deploy"}


def _age(s: float) -> str:
    s = max(float(s), 0.0)
    return f"{int(s // 60)} min" if s < 7200 else f"{s / 3600:.0f} h" if s < 172800 else f"{s / 86400:.0f} days"


def _at(ts: float, now: float) -> str:
    return time.strftime("%H:%M" if abs(ts - now) < 20 * 3600 else "%a %H:%M", time.localtime(ts))


def shown(p: Project, db=None) -> bool:
    """The queue appears in status displays: it is on, or it has rows from before it was turned off."""
    db = db or p.db
    return enabled(p) or bool(db.one("SELECT 1 FROM push_queue LIMIT 1") or db.one("SELECT 1 FROM push_batches LIMIT 1"))


def status_line(p: Project, now: float | None = None, sm: dict | None = None, db=None) -> str | None:
    """`ttp status`'s one line about the queue (None when it is not shown): what waits, the live batch,
    the last push and its deploy. Plain facts; the daemon acts on all of it, so it names no command."""
    if not shown(p, db):
        return None
    now = time.time() if now is None else now
    sm = sm or summary(p, now, db=db)
    parts = []
    n = len(sm["approved"])
    if n:
        parts.append(f"{n} approved (oldest {_age(max(a['age_s'] for a in sm['approved']))})")
    live = sm["live"]
    if live:
        k = live["rows"]
        parts.append(f"batch {PHASE_WORDS.get(live['phase'], live['phase'])} since {_age(live['age_s'])} "
                     f"({k} change{'s' if k != 1 else ''})")
    if not n and not live:
        parts.append("empty")
    last = sm["last"][0] if sm["last"] else None
    if last and last["outcome"] != "pushed" and not (live and live["id"] == last["id"]):
        parts.append(f"last batch {OUTCOME_WORDS.get(last['outcome'], last['outcome'])} "
                     f"{_age(now - (last['ended'] or last['started']))} ago")
    hold = sm.get("hold") or {}
    wait = max(float(sm.get("backoff_until") or 0), float(hold.get("until") or 0))
    if wait > now and n and not live:
        parts.append(f"next try {_at(wait, now)}")
    lp = sm.get("last_pushed")
    if lp:
        parts.append("last " + push.landing(lp["pushed_sha"], lp.get("target") or "", None, lp["version"])
                     + (f" {_age(now - lp['ended'])} ago" if lp["ended"] else "") + push.reach_words(lp.get("reach")))
        if lp["after_push"] in AFTER_WORDS and lp["after_push"] != "skipped":
            parts.append(AFTER_WORDS[lp["after_push"]])   # while it deploys, the live part says so
    return "push queue" + ("" if sm["on"] else " (off)") + ": " + " · ".join(parts)


def entries(p: Project, sm: dict, now: float, limit: int = 50, db=None) -> list[dict]:
    """The rows still in the queue and those of the batches in `sm` (a summary), newest first."""
    ids = [b["id"] for b in sm["last"]] + ([sm["live"]["id"]] if sm["live"] else [])
    rows = (db or p.db).q("SELECT q.*, t.title FROM push_queue q LEFT JOIN tasks t ON t.id=q.task WHERE q.status IN "
                  f"('approved','batched') OR q.batch IN ({','.join('?' * len(ids)) or 'NULL'}) "
                  "ORDER BY q.id DESC LIMIT ?", (*ids, limit))
    return [{"id": r["id"], "task": r["task"], "title": r["title"], "branch": r["branch"], "head": _short(r["head"]),
             "status": r["status"], "batch": r["batch"], "tries": r["tries"], "age_s": round(now - r["created"], 1),
             "pushed_sha": _short(r["pushed_sha"]) if r["pushed_sha"] else None, "version": r["version"]}
            for r in rows]


def web(p: Project, db=None, now: float | None = None) -> dict | None:
    """The web app's push queue card (None when it is not shown): the summary, the status line and
    the entries, read through the caller's connection `db`."""
    if not shown(p, db):
        return None
    now = time.time() if now is None else now
    sm = summary(p, now, last=10, db=db)
    return {**sm, "line": status_line(p, now, sm, db), "entries": entries(p, sm, now, db=db)}


def queue_text(p: Project, now: float | None = None, last: int = 10) -> str:
    """`ttp push --queue`: the open entries and the recent ones, then the last batches."""
    now = time.time() if now is None else now
    sm = summary(p, now, last=last)
    lines = [status_line(p, now, sm) or "push queue: off, nothing queued yet"]
    rows = entries(p, sm, now)
    lines.append("entries:" if rows else "entries: none")
    for r in rows:
        lines.append(f"  #{r['task']} {r['branch'] or '?'} {r['head']} {r['status']}"
                     + (f" (try {r['tries'] + 1})" if r["tries"] and r["status"] == "approved" else "")
                     + (f" in {r['batch']}" if r["batch"] and r["status"] != "approved" else "")
                     + f", {_age(r['age_s'])} old" + (f": {r['title']}" if r["title"] else ""))
    c = sm["conflicts"]
    if c["entries"]:
        lines.append(f"last {STATS_S // 86400} days: {c['entries']} entr{'ies' if c['entries'] != 1 else 'y'} done, "
                     f"{c['conflicted']} conflicted ({c['conflict_pct']}%), {c['auto_resolved']} resolved in the batch, "
                     f"{c['sent_back']} sent back ({c['sent_back_pct']}%)")
    lv = sm["live"]
    lines.append("batches, newest first:" if sm["last"] or lv else "batches: none yet")
    if lv and lv["id"] not in [b["id"] for b in sm["last"]]:   # still in its push phase
        lines.append(f"  {lv['id']} running: {PHASE_WORDS.get(lv['phase'], lv['phase'])} since {_age(lv['age_s'])}, "
                     f"{lv['rows']} change{'s' if lv['rows'] != 1 else ''}")
    for b in sm["last"]:
        what = OUTCOME_WORDS.get(b["outcome"], b["outcome"] or "?")
        if b.get("landing"):
            what = b["landing"]
        elif b["pushed_sha"]:
            what += f" {_short(b['pushed_sha'])}" + (f" as {b['version']}" if b["version"] else "")
        checks = (f", checks {b['check_runs']} run{'s' if b['check_runs'] != 1 else ''}"
                  + (f" in {b['check_s']:.0f} s" if b["check_s"] is not None else "")) if b["check_runs"] is not None else ""
        deploy = AFTER_WORDS.get(b["after_push"], b["after_push"]) if b["after_push"] else (
            f"deploying since {_age(lv['age_s'])}" if lv and lv["id"] == b["id"] else "deploy pending")
        lines.append(f"  {b['id']} {_age(now - (b['ended'] or b['started']))} ago: {what}{checks}, {deploy}")
    return "\n".join(lines)
