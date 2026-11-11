"""Open code branches and work threads, counted without a model call, with a soft cap and the
landed-branch cleanup.

Many parallel branches and workers burn money and keep changes from being tested together, so the
daemon counts them hourly (scan, stored in kv KV) and `ttp status`, the web app and the coordinator's
Course check read the count. Over `delivery.max_open_branches` or `delivery.max_work_threads` (CAPS
defaults; 0: no cap) one alarm line asks the coordinator to fold new non-urgent code work onto an open
task's branch and land open ones first. The cap holds no work: urgent and user-ordered tasks run as
before, and the dollar caps stay the hard limits.

sweep deletes the local branch of a finished task whose change is on the push branch, once its
worktree is gone (worktree.sweep, `ttp prune`), and appends each deletion with its sha to
state/branch-deletions.jsonl (RECOVERY), after saving its tip as refs/ttp-archive/<branch> (ARCHIVE) so
git gc keeps it and `git branch <name> refs/ttp-archive/<name>` restores it. It never deletes a
branch an unfinished task uses, one on a remote other than delivery.backup_remote (it may carry a PR),
one whose task has a PR, one checked out in any worktree, or one with a commit by another author than
the repo's user.email. With the same keeps it also deletes a dead branch: one whose tasks all ended
(done, cancelled or failed) more than `delivery.dead_branch_days` ago (DEAD_DAYS; 0: off) without a
push queue approval and with no unfinished task depending on them.
"""
from __future__ import annotations

import json
import subprocess
import time

from . import gitmemo, landed, tried
from .project import durable_append
from .db import TERMINAL_TASK_STATES

KV = "branchcap"
KEPT_KV = "branchcap_kept"   # {branch: sha} kept for another author: not read again at that sha;
# {branch: "sha@tip"} kept as not yet landed: read again once the push branch tip moves
DEAD_DAYS = 3
# push queue rows: approved and not yet pushed (open work), and any approval at all (never dead)
AWAITING = ("approved", "batched", "held", "conflict", "pushing")
APPROVED = AWAITING + ("pushed", "landed")
CAPS = {"max_open_branches": 6, "max_work_threads": 4}
RECOVERY = "branch-deletions.jsonl"
ARCHIVE = "refs/ttp-archive/"   # each deleted branch's tip is kept here first, so git gc never drops it
SWEEP_MAX = 50   # branch deletions per sweep: each costs a few git calls
LOOK_MAX = 200   # branches read with git per sweep, deleted or kept: the next sweep goes on
TIME_MAX_S = 8.0   # and seconds of it, so a daemon tick step stays short (the next sweep goes on)
SHOWN = 10


def _git(repo, *args, timeout=60) -> subprocess.CompletedProcess:
    try:
        return gitmemo.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def caps(cfg: dict) -> dict:
    d = (cfg or {}).get("delivery") or {}
    out = {}
    for key, default in CAPS.items():
        try:
            out[key] = max(0, int(d.get(key, default) if d.get(key) is not None else default))
        except (TypeError, ValueError):
            out[key] = default
    return out


def _live(db) -> tuple[set[int], set[int], set[int]]:
    """Tasks awaiting a push (an approved queue row not yet pushed), tasks ever approved, and tasks an
    unfinished task depends on."""
    rows = db.q("SELECT task, status FROM push_queue")
    awaiting = {r["task"] for r in rows if r["status"] in AWAITING}
    approved = {r["task"] for r in rows if r["status"] in APPROVED}
    needed: set[int] = set()
    for r in db.q("SELECT depends_on FROM tasks WHERE status NOT IN ('done','failed','cancelled') "
                  "AND depends_on IS NOT NULL AND depends_on NOT IN ('', '[]')"):
        try:
            needed.update(int(i) for i in json.loads(r["depends_on"]) or [])
        except (ValueError, TypeError):
            continue
    return awaiting, approved, needed


def _tip(p) -> str:
    try:
        return landed._push_ref(p, p.root)
    except (ValueError, OSError):
        return ""


def scan(p, now: float | None = None) -> dict:
    """Count open code branches (task branches with commits not on the push branch, not landed, of an
    unfinished task, a done task awaiting its push or one an unfinished task depends on) and running
    workers; store and return them. Branches of tasks that all ended otherwise are counted apart:
    stale (all cancelled or failed) and unlanded (done, never approved). One git call."""
    now = time.time() if now is None else now
    tip = _tip(p)
    res = _git(p.root, "for-each-ref", f"--no-merged={tip}", "--format=%(refname:short)", "refs/heads/") \
        if tip else None
    if res is None or res.returncode != 0:
        got = {"ts": now, "error": (res.stderr.strip()[:200] if res else "no push branch")}
        p.db.set_kv(KV, got)
        return got
    unmerged = set(res.stdout.split())
    done = tried.landed_tasks(p.db)
    by_branch: dict[str, list[dict]] = {}
    for t in p.db.q("SELECT id, status, branch FROM tasks WHERE kind='code' AND branch IS NOT NULL "
                    "AND branch != '' ORDER BY id"):
        by_branch.setdefault(t["branch"], []).append(t)
    awaiting, _, needed = _live(p.db)
    open_ids, stale, unlanded = [], 0, 0
    for branch, ts in by_branch.items():
        if branch not in unmerged or any(t["id"] in done for t in ts):
            continue
        if any(t["status"] not in TERMINAL_TASK_STATES or t["id"] in awaiting or t["id"] in needed for t in ts):
            open_ids.append(ts[-1]["id"])
        elif all(t["status"] in ("cancelled", "failed") for t in ts):
            stale += 1
        else:
            unlanded += 1
    threads = [r["id"] for r in p.db.q("SELECT id FROM tasks WHERE status='running' ORDER BY id")]
    got = {"ts": now, "open_branches": len(open_ids), "open_tasks": open_ids[-SHOWN:],
           "stale_branches": stale, "unlanded_branches": unlanded, "threads": len(threads), "thread_tasks": threads[-SHOWN:]}
    p.db.set_kv(KV, got)
    return got


def _over(db, cfg) -> tuple[dict, dict, list[str]]:
    got = db.kv(KV, {}) or {}
    c = caps(cfg)
    over = []
    if c["max_open_branches"] and got.get("open_branches", 0) > c["max_open_branches"]:
        over.append(f"{got['open_branches']} open code branches (cap {c['max_open_branches']})")
    if c["max_work_threads"] and got.get("threads", 0) > c["max_work_threads"]:
        over.append(f"{got['threads']} running workers (cap {c['max_work_threads']})")
    return got, c, over


def course_key(db, cfg) -> str:
    """Which caps are exceeded ('branches', 'threads'), not the counts, so the Course check stays
    '(as last turn)' while only the numbers move."""
    got, c, _ = _over(db, cfg)
    return ",".join(name for name, key, cap in (("branches", "open_branches", "max_open_branches"),
                                                 ("threads", "threads", "max_work_threads"))
                    if c[cap] and got.get(key, 0) > c[cap])


def status_line(db, cfg) -> str:
    """`ttp status`: the counts, with the alarm when over a cap; '' before the first scan."""
    got, c, over = _over(db, cfg)
    if "open_branches" not in got:
        return ""
    line = (f"Branches: {got['open_branches']} open code branches (cap {c['max_open_branches'] or 'none'}), "
            f"{got['threads']} workers running (cap {c['max_work_threads'] or 'none'})")
    if got.get("stale_branches"):
        line += f", {got['stale_branches']} unlanded branches of cancelled or failed tasks"
    if got.get("unlanded_branches"):
        line += f", {got['unlanded_branches']} unlanded branches of done tasks never approved"
    return line + (" | OVER CAP: fold new code work onto open branches" if over else "")


def digest_lines(db, cfg) -> list[str]:
    """The Course check's alarm line, only when over a cap."""
    got, _, over = _over(db, cfg)
    if not over:
        return []
    ids = ", ".join(f"#{i}" for i in got.get("open_tasks", []))
    return [f"- open branches: {' and '.join(over)}{f' (latest: {ids})' if ids else ''} | consolidate: give new "
            "non-urgent code work whose files overlap an open code task that task's branch (`pr_branch:`) or fold "
            "it into that task's spec, and land open branches before opening more; urgent and user-ordered work "
            "is never held by this cap"]


def sweep(p, cfg, now: float | None = None, dry: bool = False, limit: int = SWEEP_MAX,
          time_max: float = TIME_MAX_S) -> list[dict]:
    """Delete landed and dead tasks' local branches (see the module doc); returns what it deleted (or would)."""
    now = time.time() if now is None else now
    d = (cfg or {}).get("delivery") or {}
    do_landed = d.get("delete_landed_branches") is not False
    try:
        dead_s = max(0.0, float(d.get("dead_branch_days", DEAD_DAYS) if d.get("dead_branch_days") is not None
                                else DEAD_DAYS)) * 86400
    except (TypeError, ValueError):
        dead_s = DEAD_DAYS * 86400
    if not do_landed and not dead_s:
        return []
    repo, bump = p.root, d.get("version_bump")
    me = _git(repo, "config", "user.email").stdout.strip().lower()
    tip = _tip(p)
    if not me or not tip:
        return []
    local = dict(ln.split(" ", 1)[::-1] for ln in _git(
        repo, "for-each-ref", "--format=%(objectname) %(refname:short)", "refs/heads/").stdout.splitlines() if " " in ln)
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    tip_sha = _git(repo, "rev-parse", "-q", "--verify", tip).stdout.strip()
    kept_before = p.db.kv(KEPT_KV, {}) or {}
    kept = {b: v for b, v in kept_before.items() if v in (local.get(b), f"{local.get(b)}@{tip_sha}")}
    # Checked out in any worktree (a task's, a batch's, the project root's): update-ref would not refuse it.
    out_wt = {ln[len("branch refs/heads/"):] for ln in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
              if ln.startswith("branch refs/heads/")}
    backup = str(d.get("backup_remote") or "")
    remote = set()
    for ref in _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/").stdout.split():
        name, _, branch = ref.partition("/")
        if name != backup:
            remote.add(branch)
    done = tried.landed_tasks(p.db)
    merged = set(_git(repo, "for-each-ref", f"--merged={tip}", "--format=%(refname:short)", "refs/heads/").stdout.split())
    pushed = {r["head"] for r in p.db.q("SELECT head FROM push_queue WHERE status IN ('pushed','landed') "
                                        "AND COALESCE(landed_sha, pushed_sha) IS NOT NULL")}
    by_branch: dict[str, list[dict]] = {}
    _, approved, needed = _live(p.db)
    for t in p.db.q("SELECT id, status, branch, pr_url, updated FROM tasks WHERE branch IS NOT NULL "
                    "AND branch != '' ORDER BY id"):
        by_branch.setdefault(t["branch"], []).append(t)
    out: list[dict] = []
    looked, start = 0, time.monotonic()
    for branch, ts in by_branch.items():
        if len(out) >= limit or looked >= LOOK_MAX or time.monotonic() - start > time_max:
            break
        sha = local.get(branch)
        is_landed = do_landed and any(t["id"] in done for t in ts)
        # Dead: never landed or approved, nothing unfinished needs it, all ended over dead_s ago.
        is_dead = (dead_s and not any(t["id"] in done or t["id"] in approved or t["id"] in needed for t in ts)
                   and all(now - float(t.get("updated") or now) > dead_s for t in ts))
        if (not sha or branch in kept or branch in remote or branch in out_wt or not (is_landed or is_dead)
                or any(t["status"] not in TERMINAL_TASK_STATES or t.get("pr_url") for t in ts)
                or any((p.worktrees / f"t{t['id']}").exists() for t in ts)):
            continue
        looked += 1
        # Its own commits: since it was created (its reflog's first entry), else those not on the tip.
        authors = _git(repo, "log", "--format=%ae", f"{_created(common, branch) or tip}..{sha}")
        if authors.returncode != 0:
            continue
        # By this repo's user only, and on the tip, or the very head the push queue landed, or landed rebased.
        if any(a.strip().lower() != me for a in authors.stdout.split()):
            kept[branch] = sha
            continue
        if is_landed and not (branch in merged or sha in pushed or landed.on_push_branch(p, branch)
                              or landed.equivalent(p, branch, bump)):
            kept[branch] = f"{sha}@{tip_sha}"   # read again once the tip moves
            continue
        row = {"ts": now, "task": ts[-1]["id"], "branch": branch, "sha": sha, "why": "landed" if is_landed else "dead",
               "saved": ARCHIVE + branch}
        if not dry:
            if (_git(repo, "update-ref", ARCHIVE + branch, sha).returncode != 0
                    or _git(repo, "update-ref", "-d", f"refs/heads/{branch}", sha).returncode != 0):
                continue
            durable_append(p.state / RECOVERY, json.dumps(row) + "\n")
        out.append(row)
    if not dry and kept != kept_before:
        p.db.set_kv(KEPT_KV, kept)
    return out


def _created(common: str, branch: str) -> str:
    """The commit a branch was created at: its reflog's first entry; '' when it has none."""
    try:
        with open(f"{common}/logs/refs/heads/{branch}", encoding="utf-8", errors="replace") as f:
            first = f.readline().split()
    except OSError:
        return ""
    return first[1] if len(first) > 1 and len(first[1]) >= 40 else ""
