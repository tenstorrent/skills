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
state/branch-deletions.jsonl (RECOVERY) so `git branch <name> <sha>` restores it. It never deletes a
branch an unfinished task uses, one on a remote other than delivery.backup_remote (it may carry a PR),
one whose task has a PR, or one with a commit by another author than the repo's user.email.
"""
from __future__ import annotations

import json
import subprocess
import time

from . import landed, tried
from .db import TERMINAL_TASK_STATES

KV = "branchcap"
KEPT_KV = "branchcap_kept"   # {branch: sha} sweep read and kept (another author, not landed): not read again at that sha
CAPS = {"max_open_branches": 8, "max_work_threads": 6}
RECOVERY = "branch-deletions.jsonl"
SWEEP_MAX = 50   # branch deletions per sweep: each costs a few git calls
LOOK_MAX = 200   # branches read with git per sweep, deleted or kept: the next sweep goes on
TIME_MAX_S = 8.0   # and seconds of it, so a daemon tick step stays short (the next sweep goes on)
SHOWN = 10


def _git(repo, *args, timeout=60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
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


def _tip(p) -> str:
    try:
        return landed._push_ref(p, p.root)
    except (ValueError, OSError):
        return ""


def scan(p, now: float | None = None) -> dict:
    """Count open code branches (task branches with commits not on the push branch, of tasks not
    landed, cancelled or failed) and running workers; store and return them. One git call."""
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
    open_ids, stale = [], 0
    for branch, ts in by_branch.items():
        if branch not in unmerged or any(t["id"] in done for t in ts):
            continue
        if all(t["status"] in ("cancelled", "failed") for t in ts):
            stale += 1
        else:
            open_ids.append(ts[-1]["id"])
    threads = [r["id"] for r in p.db.q("SELECT id FROM tasks WHERE status='running' ORDER BY id")]
    got = {"ts": now, "open_branches": len(open_ids), "open_tasks": open_ids[-SHOWN:],
           "stale_branches": stale, "threads": len(threads), "thread_tasks": threads[-SHOWN:]}
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


def status_line(db, cfg) -> str:
    """`ttp status`: the counts, with the alarm when over a cap; '' before the first scan."""
    got, c, over = _over(db, cfg)
    if "open_branches" not in got:
        return ""
    line = (f"Branches: {got['open_branches']} open code branches (cap {c['max_open_branches'] or 'none'}), "
            f"{got['threads']} workers running (cap {c['max_work_threads'] or 'none'})")
    if got.get("stale_branches"):
        line += f", {got['stale_branches']} unlanded branches of cancelled or failed tasks"
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
    """Delete landed tasks' local branches (see the module doc); returns what it deleted (or would)."""
    now = time.time() if now is None else now
    d = (cfg or {}).get("delivery") or {}
    if d.get("delete_landed_branches") is False:
        return []
    repo, bump = p.root, d.get("version_bump")
    me = _git(repo, "config", "user.email").stdout.strip().lower()
    tip = _tip(p)
    if not me or not tip:
        return []
    local = dict(ln.split(" ", 1)[::-1] for ln in _git(
        repo, "for-each-ref", "--format=%(objectname) %(refname:short)", "refs/heads/").stdout.splitlines() if " " in ln)
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    kept_before = p.db.kv(KEPT_KV, {}) or {}
    kept = {b: sha for b, sha in kept_before.items() if local.get(b) == sha}
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
    for t in p.db.q("SELECT id, status, branch, pr_url FROM tasks WHERE branch IS NOT NULL AND branch != '' "
                    "ORDER BY id"):
        by_branch.setdefault(t["branch"], []).append(t)
    out: list[dict] = []
    looked, start = 0, time.monotonic()
    for branch, ts in by_branch.items():
        if len(out) >= limit or looked >= LOOK_MAX or time.monotonic() - start > time_max:
            break
        sha = local.get(branch)
        if (not sha or kept.get(branch) == sha or branch in remote or not any(t["id"] in done for t in ts)
                or any(t["status"] not in TERMINAL_TASK_STATES or t.get("pr_url") for t in ts)
                or any((p.worktrees / f"t{t['id']}").exists() for t in ts)):
            continue
        looked += 1
        # Its own commits: since it was created (its reflog's first entry), else those not on the tip.
        authors = _git(repo, "log", "--format=%ae", f"{_created(common, branch) or tip}..{sha}")
        if authors.returncode != 0:
            continue
        # By this repo's user only, and on the tip, or the very head the push queue landed, or landed rebased.
        if any(a.strip().lower() != me for a in authors.stdout.split()) or not (
                branch in merged or sha in pushed or landed.on_push_branch(p, branch)
                or landed.equivalent(p, branch, bump)):
            kept[branch] = sha
            continue
        row = {"ts": now, "task": ts[-1]["id"], "branch": branch, "sha": sha, "why": "landed"}
        if not dry:
            if _git(repo, "update-ref", "-d", f"refs/heads/{branch}", sha).returncode != 0:
                continue
            with open(p.state / RECOVERY, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
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
