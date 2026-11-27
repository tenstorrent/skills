# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A PR-branch delivery judged whole: the review prompt's PR facts (model-free).

A change that delivers onto an open PR's branch is judged on the PR's cumulative diff against its
merge base, not on this task's commits alone: a PR can grow far past its purpose one reviewed
commit at a time. The budget gate (budget_gate) refuses a push onto a branch whose open PR
is past its size budget, raising one keyed event so the coordinator posts a plan; a recorded
plan (record_plan, the `ttp pr plan` CLI) lifts the gate.

review_prompt_lines builds the block the review prompt carries: the PR's whole diffstat (lines
added and removed, split into code, tests, docs and generated files), the repo's norm (median and
p90 added lines of its recently merged PRs, read anonymously through the gh route and cached per
repo for a day; 'norm unknown' when the route cannot answer) and the ponytail lane's duty to judge
the PR as a whole. Nothing is added when the change heads no open PR."""
from __future__ import annotations

import time
from pathlib import PurePosixPath

NORM_TTL_S = 86400.0      # a repo's norm is cached a day
NORM_PRS = 8               # how many of the repo's recently merged PRs the norm reads
NORM_STEP_S = 25.0        # the norm read's whole budget, so queueing a review never hangs
LOCKS = {"package-lock.json", "go.sum"}   # lockfiles: generated, whatever their extension


def _gh_get(path: str, ttl: float):
    """The gh route's JSON answer for `path`, or None when the route cannot answer."""
    from . import ghanon
    try:
        return ghanon.get(path, ttl, key="prsize " + path)
    except (ghanon.Spent, ghanon.Unreadable, OSError, ValueError):
        return None


def _pr_record(d, repo: str, n: int) -> dict | None:
    """The open PR record of a REST answer, or None when the PR is not open or is unreadable."""
    if not isinstance(d, dict) or str(d.get("state") or "") != "open" or not n:
        return None
    title = str(d.get("title") or "")
    return {"repo": repo, "number": n, "url": str(d.get("html_url") or f"{repo}#{n}"),
            "title": title[:120] + ("…" if len(title) > 120 else ""),
            "branch": str((d.get("head") or {}).get("ref") or "")}


def open_pr(p, task: dict, elsewhere: str) -> dict | None:
    """The open PR (draft included) the change delivers onto: the task's own PR first, else the open
    PR whose head branch is the pr_branch (`elsewhere`) it delivers onto. None when neither heads one."""
    from . import ghroute, prguard
    if key := prguard.pr_key(prguard.task_pr(task) or ""):
        repo, _, n = key.rpartition("#")
        if pr := _pr_record(_gh_get(f"repos/{repo}/pulls/{n}", 25 * 60), repo, int(n)):
            return pr
    if not elsewhere:
        return None
    repo = ghroute.repo_of(str(p.root))
    if "/" not in repo:
        return None
    found = _gh_get(f"repos/{repo}/pulls?head={repo.split('/', 1)[0]}:{elsewhere}&state=open&per_page=1",
                 25 * 60)
    for d in found if isinstance(found, list) else []:
        if pr := _pr_record(d, repo, int((d or {}).get("number") or 0)):
            return pr
    return None


def file_bucket(path: str) -> str:
    """Which diffstat bucket a file lands in: tests, generated files, docs, else code."""
    from . import slop
    parts = PurePosixPath(path).parts
    if slop._is_test(path):
        return "tests"
    name = PurePosixPath(path).name
    if any(seg in slop.SKIP_DIRS for seg in parts[:-1]) or name.endswith(".lock") or name in LOCKS:
        return "generated"
    if parts[0] in ("doc", "docs") or PurePosixPath(path).suffix in (".md", ".rst", ".txt"):
        return "docs"
    return "code"


def cumulative_stat(p, ref: str) -> dict | None:
    """Branch `ref`'s cumulative diff against its merge base with its base branch (the remote's HEAD,
    else the push branch): {adds, dels, files, base} and the same per category, or None when the
    project's git cannot tell."""
    from . import worktree
    push = str(((p.config().get("delivery") or {}).get("push_branch")) or "")
    for base in ("origin/HEAD", *((f"origin/{push}",) if push else ())):
        try:
            rows = worktree._git(p.root, "diff", "--numstat", "--no-renames",
                                 f"{base}...{ref}").splitlines()
        except (RuntimeError, OSError):
            continue
        out = {"adds": 0, "dels": 0, "files": 0, "base": base,
               "code": [0, 0, 0], "tests": [0, 0, 0], "docs": [0, 0, 0], "generated": [0, 0, 0]}
        for row in rows:
            plus, minus, path = (row.split("\t", 2) + ["", "", ""])[:3]
            if not path:
                continue
            cat = out[file_bucket(path)]
            out["files"], cat[2] = out["files"] + 1, cat[2] + 1
            if plus != "-":
                out["adds"], cat[0] = out["adds"] + int(plus), cat[0] + int(plus)
                out["dels"], cat[1] = out["dels"] + int(minus), cat[1] + int(minus)
        return out
    return None


def _quantile(sorted_vals: list[int], q: float) -> int:
    return sorted_vals[min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1) + 0.5))]


def repo_norm(repo: str) -> dict | None:
    """The repo's norm: {median, p90, n} added lines over its recently merged PRs, read anonymously
    and cached per repo for a day. None when the route cannot answer."""
    found = _gh_get(f"repos/{repo}/pulls?state=closed&per_page=100&sort=updated&direction=desc",
                 NORM_TTL_S)
    nums = [int(d.get("number")) for d in found if isinstance(d, dict) and d.get("merged_at")
            and isinstance(d.get("number"), int)][:NORM_PRS] if isinstance(found, list) else []
    adds, end = [], time.monotonic() + NORM_STEP_S
    for n in nums:
        if time.monotonic() > end:
            break
        d = _gh_get(f"repos/{repo}/pulls/{n}", NORM_TTL_S)
        if isinstance(d, dict) and isinstance(d.get("additions"), int):
            adds.append(d["additions"])
    if len(adds) < 2:
        return None
    adds.sort()
    mid = len(adds) // 2
    return {"median": (adds[mid] if len(adds) % 2 else (adds[mid - 1] + adds[mid]) // 2),
            "p90": _quantile(adds, 0.9), "n": len(adds)}


# --- the PR size budget ---------------------------------------------------------------------

PLAN_KEY = "pr-size-plans"    # db kv: the recorded plans {"owner/repo#N": {"budget", "msg", "at"}}
BUDGET_EVENT = "pr_size_budget"   # one keyed event when a push passes a PR's budget
DEFAULT_BUDGET = 800   # added non-test lines when a repo's norm cannot be read: about twice the
                        # ponytail landing cap, so a PR of a few tasks passes and a runaway one is caught


def budget_of(p, repo: str) -> int:
    """The PR size budget of `repo`, in added non-test lines: review.pr_budget_lines when set
    (0: the gate is off), else the repo norm's p90, else DEFAULT_BUDGET. Off in the test suite's
    size-gate-off mode (ponytail._test_off), like the landing size gate."""
    from . import ponytail
    if ponytail._test_off():
        return 0
    v = (((p.config() or {}).get("review") or {}).get("pr_budget_lines")) if p is not None else None
    try:
        if v not in (None, ""):
            return max(0, int(v))
    except (TypeError, ValueError):
        pass
    n = repo_norm(repo)
    return int(n["p90"]) if n else DEFAULT_BUDGET


def plan_budget(db, key: str) -> int | None:
    """The budget a recorded plan lifted the PR size gate to for `key` ("owner/repo#N"), else None."""
    row = (db.kv(PLAN_KEY, {}) or {}).get(key.lower()) or {}
    try:
        return int(row["budget"])
    except (KeyError, TypeError, ValueError):
        return None


def record_plan(db, key: str, budget: int, msg: str) -> None:
    """Record a PR size plan (`ttp pr plan`): the new budget that lifts the gate for `key`
    ("owner/repo#N"), with the chat message id of the plan the coordinator posted to the user."""
    plans = db.kv(PLAN_KEY, {}) or {}
    plans[key.lower()] = {"budget": int(budget), "msg": str(msg or ""), "at": time.time()}
    db.set_kv(PLAN_KEY, plans)


def _budget_event(p, pr: dict, size: int, budget: int) -> None:
    """One keyed event (one per PR and budget) naming the PR, its size, the norm and the budget,
    so the coordinator posts a plan (`ttp pr plan`) that lifts the gate."""
    key = f"{pr['repo']}#{pr['number']}"
    fp = f"{BUDGET_EVENT}:{key}:{budget}"
    if p.db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
        return
    n = repo_norm(pr["repo"])
    norm = (f"median {n['median']}, p90 {n['p90']} added lines over {n['n']} recent merges"
            if n else "unknown (the gh route did not answer)")
    text = (f"PR {pr['url']} (\"{pr['title']}\") is over its size budget: {size} non-test added "
            f"lines against a budget of {budget}; the repo's merged-PR norm: {norm}. No further "
            f"commits may land on its branch until a plan is recorded: "
            f"`ttp pr plan {key} --budget <lines> --msg <chat message id>`")
    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
           (time.time(), "pr", BUDGET_EVENT, fp, "normal", text, "queued"))


def budget_gate(p, task: dict | None, branch: str) -> str:
    """Why a push onto `branch` is refused ("" when it is not): its open PR's cumulative added
    non-test lines are past the budget (review.pr_budget_lines, else the repo norm's p90, else
    DEFAULT_BUDGET; a recorded plan's budget lifts the gate). Raises one keyed event naming the
    PR, its size, the norm and the budget the first time a budget is passed."""
    if p is None or not branch:
        return ""
    try:
        pr = open_pr(p, task or {}, branch)
        if not pr or pr["branch"] != branch:
            return ""
        st = cumulative_stat(p, branch)
        if not st:
            return ""
        budget = plan_budget(p.db, f"{pr['repo']}#{pr['number']}")
        budget = budget if budget is not None and budget > 0 else budget_of(p, pr["repo"])
        if budget <= 0:
            return ""
        size = st["adds"] - st["tests"][0]
        if size <= budget:
            return ""
        _budget_event(p, pr, size, budget)
        return (f"{branch} heads {pr['url']}, which is over its size budget: {size} non-test "
                f"added lines against a budget of {budget}. No further commits may land on it "
                f"until a plan is recorded: `ttp pr plan {pr['repo']}#{pr['number']} "
                f"--budget <lines> --msg <chat message id>`")
    except Exception as e:
        from .daemon import log
        if p is not None:
            log(p, f"the PR size gate could not read the PR headed by {branch} ({e})")
        return ""


def _stat_line(st: dict, ref: str) -> str:
    cats = ", ".join(f"{name} +{st[name][0]}/-{st[name][1]} ({st[name][2]} files)"
                      for name in ("code", "tests", "docs", "generated"))
    return (f"Its cumulative diff since its merge base is +{st['adds']}/-{st['dels']} across "
            f"{st['files']} files: {cats}. The full diff: `git diff {st['base']}...{ref}`.")


def review_prompt_lines(p, task: dict, elsewhere: str, head: str) -> list[str]:
    """The review prompt's PR block for a change delivering onto `elsewhere` (its pr_branch) or
    carrying its own open PR: [] when the change heads no open PR or the facts cannot be read (a
    review is never refused its spec over them)."""
    try:
        return _review_prompt_lines(p, task, elsewhere, head)
    except Exception as e:
        from .daemon import log
        log(p, f"task {task.get('id')}: its PR size facts were not read ({e})")
        return []


def _review_prompt_lines(p, task: dict, elsewhere: str, head: str) -> list[str]:
    pr = open_pr(p, task, elsewhere)
    if not pr:
        return []
    first = (f"PR {pr['url']} (\"{pr['title']}\") heads this change: judge the whole PR's cumulative "
             f"diff against its merge base, not only this change's own commits.")
    for ref in (elsewhere, pr["branch"], head):
        if st := cumulative_stat(p, ref):
            second = _stat_line(st, ref)
            break
    else:
        second = "Its cumulative diff could not be read here: read it with `gh pr diff`."
    n = repo_norm(pr["repo"])
    third = (f"The repo's norm for merged PRs: median {n['median']}, p90 {n['p90']} added lines "
             f"over {n['n']} recent merges." if n else
             "The repo's norm is unknown (the gh route did not answer): judge its size against the "
             "configured size limits alone.")
    fourth = ("The ponytail lane's verdict covers the PR as a whole: does it still do what its title "
              "and description say, with the least code? Not only whether this commit is clean.")
    return [first, second, third, fourth]
