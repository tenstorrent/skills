"""Shared-branch integration: compatible changes share one branch and one review (user msgs #2407,
#2414). Model-free helpers the daemon uses:

- integration_branch: the one branch per repo reviewed changes land on: the push branch on a
  push-branch project, else delivery.integration_branch (INTEGRATION_DEFAULT), carried by one draft PR.
- members / members_block: the done code tasks one review of a shared branch covers, each with its
  acceptance criteria, so each finding names the task it is about (finding_member).
- record_member_head / member_commits / revert_member: the commits a member added to the shared branch
  (its fork point to the head it handed off), and their forward revert, so the rest can land while the
  failed member goes back alone.
- route: whether a change merges cleanly onto the integration branch (shared) or keeps its own branch:
  a real conflict, or a `no_push:` line in its spec.
- status_lines: per repo, the integration branch, what is on it and not landed, and what was kept on
  a branch of its own with the conflict that put it there."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import worktree
from .db import dependency_ids

INTEGRATION_DEFAULT = "ttp/integration"
MEMBER_HEAD_REF = "refs/ttp/member-head/t"   # + task id: the head a member of a shared branch handed off
MEMBERS_PREFIX = "Members of this review (they share one branch; review the head as one change):"
OWN_BRANCH_LABEL = "own_branch:"   # + why: a change kept on a branch of its own (a conflict, no_push)
REVERT_TRAILER = "Ttp-Reverted-Task"
_ACCEPT = re.compile(r"^\s*(?:[-*]\s*)?(acceptance(?: criteria)?|accept|done when)\s*[:\-]", re.I)
_MEMBER = re.compile(r"\btask #(\d+)\b", re.I)
_NO_PUSH = re.compile(r"^\s*no_push:", re.I | re.M)


def integration_branch(cfg: dict) -> str:
    """The branch compatible reviewed changes land on: the push branch where one is set (it is the
    integration branch), else delivery.integration_branch, else INTEGRATION_DEFAULT."""
    d = cfg.get("delivery") or {}
    return str(d.get("push_branch") or d.get("integration_branch") or INTEGRATION_DEFAULT).strip()


def is_pr_project(cfg: dict) -> bool:
    """Whether reviewed changes reach the integration branch through one draft PR (no push branch)."""
    return not (cfg.get("delivery") or {}).get("push_branch")


def acceptance(spec: str | None, limit: int = 400) -> str:
    """A task spec's acceptance criteria: the paragraph starting at its first 'Acceptance:' (or 'done
    when:') line, else the start of the spec, clipped to `limit` characters on one line."""
    lines = (spec or "").splitlines()
    start = next((i for i, ln in enumerate(lines) if _ACCEPT.match(ln)), None)
    if start is not None:
        block = [lines[start]]
        for ln in lines[start + 1:]:
            if not ln.strip():
                break
            block.append(ln)
        text = " ".join(s.strip() for s in block)
    else:
        text = " ".join(s.strip() for s in lines if s.strip())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def members(db, review: dict) -> list[dict]:
    """The code tasks review `review` covers (its dependencies), in id order."""
    out = []
    for i in sorted({i for i in dependency_ids(review) if isinstance(i, int)}):
        t = db.task(i)
        if t and t.get("kind") == "code":
            out.append(t)
    return out


def members_block(tasks: list[dict], tip: tuple[str, str] | None = None) -> str:
    """The members block a shared-branch review's spec carries: each task's id, title and acceptance
    criteria, the head that carries them all (`tip`: branch, commit), and how to attribute findings."""
    lines = [MEMBERS_PREFIX]
    if tip:
        lines.append(f"Review head {tip[1][:12]} of {tip[0]}: it carries every member's commits.")
    for t in tasks:
        lines.append(f"- task #{t['id']} ({t.get('title') or ''}) on {t.get('branch') or '?'}: "
                     f"acceptance: {acceptance(t.get('spec'))}")
    lines.append("Check each member against its own acceptance criteria. Start each finding's title with the "
                 "member it is about, as `task #<id>:`; a finding names one member only. When some members pass "
                 "and others fail, hand off `failed` with `members` mapping each member id to pass or fail, put "
                 "only the passing members' own heads in `push`, and give the failed members' findings as "
                 "follow-ups. Members that pass are approved together; the fix covers the failed part only.")
    return "\n".join(lines)


def with_members(spec: str, tasks: list[dict], tip: tuple[str, str] | None = None) -> str:
    """`spec` with its members block replaced by (or, with none yet, followed by) the block for `tasks`."""
    spec = spec or ""
    i = spec.find(MEMBERS_PREFIX)
    if i >= 0:
        # The block ends at its last line, the attribution rule.
        end = spec.find("\n", spec.find("Members that pass are approved together", i))
        spec = spec[:i].rstrip("\n") + (spec[end:] if end >= 0 else "")
    return f"{spec.rstrip()}\n{members_block(tasks, tip)}"


def finding_member(fup: dict, ids: list[int]) -> int | None:
    """The member a review finding names (`task #<id>` in its title, else its spec), when it is one of
    `ids`; with one member, that one. None when it names no member, or more than one in its title."""
    if len(ids) == 1:
        return ids[0]
    for field in ("title", "spec"):
        named = {int(m) for m in _MEMBER.findall(str(fup.get(field) or ""))} & set(ids)
        if len(named) == 1:
            return named.pop()
        if named:
            return None
    return None


def by_member(fups: list[dict], ids: list[int]) -> tuple[dict[int, list[dict]], list[dict]]:
    """Findings grouped by the member each names, and those naming none (the branch as a whole)."""
    out: dict[int, list[dict]] = {}
    loose: list[dict] = []
    for f in fups:
        if not isinstance(f, dict):
            continue
        m = finding_member(f, ids)
        (out.setdefault(m, []) if m is not None else loose).append(f)
    return out, loose


def _is_ancestor(repo: Path, commit: str, head: str) -> bool:
    try:
        return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, head],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


PASS_WORDS = {"pass", "passed", "ok", "approve", "approved", "accept", "accepted", "done"}


def verdicts(ids: list[int], result: dict | None, fups: list[dict]) -> dict[int, str] | None:
    """One verdict per member of a shared-branch review: {id: "pass" | "fail"}. The reviewer's own
    `members` map in its hand-off ({"<id>": "pass"|"fail"}) decides for the members it names; any other
    member fails when a finding names it and passes when none does. None when this is no split: fewer
    than two members, a finding naming no member (the branch as a whole), or no member failing."""
    if len(ids) < 2:
        return None
    grouped, loose = by_member([f for f in fups if isinstance(f, dict)], ids)
    if loose:
        return None
    given = (result or {}).get("members")
    out: dict[int, str] = {}
    for i in ids:
        v = ""
        if isinstance(given, dict):
            v = str(given.get(str(i), given.get(i, ""))).strip().lower()
        out[i] = ("pass" if v in PASS_WORDS else "fail") if v else ("fail" if i in grouped else "pass")
    return out if "fail" in out.values() else None


def landable(repo: Path, ids: list[int], vs: dict[int, str]) -> list[int]:
    """The passing members that can be approved on their own head: the head each handed off is recorded
    and carries no commit of a failing member (an earlier member's bad commits sit under a later head,
    which waits for the fix's re-review)."""
    bad: set[str] = set()
    for i in ids:
        if vs.get(i) == "fail":
            bad.update(member_commits(repo, i))
    out = []
    for i in ids:
        head = member_head(repo, i)
        if vs.get(i) != "pass" or not head:
            continue
        if any(_is_ancestor(repo, c, head) for c in bad):
            continue
        out.append(i)
    return out


def scope_text(vs: dict[int, str]) -> str:
    """The line a fix or re-review spec carries for a split verdict: only the failed members' parts."""
    fail = [i for i, v in sorted(vs.items()) if v == "fail"]
    ok = [i for i, v in sorted(vs.items()) if v == "pass"]
    return (f"Only {', '.join(f'task #{i}' for i in fail)}'s part failed review: fix only its findings below. "
            f"{', '.join(f'task #{i}' for i in ok)} passed and must not change.")


def record_member_head(repo: Path, task_id: int, head: str) -> None:
    """Record `head` as the head member task `task_id` handed off on its shared branch."""
    if head:
        worktree._git(repo, "update-ref", f"{MEMBER_HEAD_REF}{task_id}", head, check=False)


def member_head(repo: Path, task_id: int) -> str:
    return worktree._git(repo, "rev-parse", "--verify", "--quiet", f"{MEMBER_HEAD_REF}{task_id}^{{commit}}",
                         check=False)


def member_commits(repo: Path, task_id: int, after: str = "") -> list[str]:
    """The commits member `task_id` added: from its fork point (or `after`, the head the previous member
    ended on) to the head it handed off, oldest first, merges left out. [] when either end is unknown."""
    start = after or worktree.fork_point(repo, task_id)
    end = member_head(repo, task_id)
    if not start or not end:
        return []
    out = worktree._git(repo, "rev-list", "--reverse", "--no-merges", f"{start}..{end}", check=False)
    return [c for c in out.split() if c]


def _identity(cwd: Path) -> list[str]:
    """`-c` options naming a committer when the repository has none configured."""
    if worktree._git(cwd, "config", "user.email", check=False):
        return []
    return ["-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost"]


def _checked_out(repo: Path, branch: str) -> Path | None:
    out = worktree._git(repo, "worktree", "list", "--porcelain", check=False)
    path = None
    for ln in out.splitlines():
        if ln.startswith("worktree "):
            path = Path(ln[len("worktree "):])
        elif ln == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def revert_member(repo: Path, branch: str, task_id: int, commits: list[str]) -> tuple[bool, str]:
    """Revert member `task_id`'s `commits` forward on `branch` as one commit (REVERT_TRAILER), so the
    other members can land without it: (True, new head) or (False, why). Never rewrites history: the
    branch only moves forward, by compare-and-swap when no worktree has it checked out. A conflict, a
    dirty worktree holding the branch or a branch that moved meanwhile leaves everything as it was."""
    if not commits:
        return False, f"no commits of task #{task_id} on record"
    old = worktree._git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if not old:
        return False, f"branch {branch} not found"
    for c in commits:
        if subprocess.run(["git", "merge-base", "--is-ancestor", c, old], cwd=repo,
                          capture_output=True, timeout=120).returncode:
            return False, f"commit {c[:12]} of task #{task_id} is not on {branch}"
    held = _checked_out(repo, branch)
    if held is not None and worktree._git(held, "status", "--porcelain", "--untracked-files=no", check=False):
        return False, f"{branch} is checked out at {held} with uncommitted changes"
    tmp = None if held is not None else Path(tempfile.mkdtemp(prefix="ttp-revert-"))
    cwd = held if held is not None else tmp / "wt"
    try:
        if tmp is not None:
            worktree._git(repo, "worktree", "add", "--detach", str(cwd), old)
        ident = _identity(cwd)
        r = subprocess.run(["git", *ident, "revert", "--no-commit", *reversed(commits)], cwd=cwd,
                           capture_output=True, text=True, timeout=120)
        if r.returncode:
            subprocess.run(["git", "revert", "--abort"], cwd=cwd, capture_output=True, timeout=60)
            subprocess.run(["git", "reset", "--hard", "-q", old], cwd=cwd, capture_output=True, timeout=60)
            return False, f"reverting task #{task_id} conflicts: {(r.stderr or r.stdout).strip()[:300]}"
        msg = (f"Revert task #{task_id} from shared branch {branch}\n\nIts review failed; the other members "
               f"land without it and it goes back alone.\n\n{REVERT_TRAILER}: {task_id}")
        worktree._git(cwd, *ident, "commit", "-q", "--allow-empty", "-m", msg)
        new = worktree._git(cwd, "rev-parse", "HEAD")
        if tmp is not None and subprocess.run(
                ["git", "update-ref", f"refs/heads/{branch}", new, old], cwd=repo,
                capture_output=True, timeout=60).returncode:
            return False, f"{branch} moved while task #{task_id} was reverted; nothing changed"
        return True, new
    finally:
        if tmp is not None:
            worktree._git(repo, "worktree", "remove", "--force", str(cwd), check=False)
            shutil.rmtree(tmp, ignore_errors=True)


def conflicts(repo: Path, onto: str, head: str) -> list[str] | None:
    """The files merging `head` onto `onto` conflicts in ([] merges cleanly), or None when it could not
    be told. Uses `git merge-tree --write-tree` (git 2.38+), else a trial merge in a scratch worktree."""
    r = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", onto, head],
                       cwd=repo, capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        return []
    if r.returncode == 1 and r.stdout.strip():
        return [ln for ln in r.stdout.splitlines()[1:] if ln.strip()]
    tmp = Path(tempfile.mkdtemp(prefix="ttp-mergecheck-"))
    cwd = tmp / "wt"
    try:
        if subprocess.run(["git", "worktree", "add", "--detach", str(cwd), onto], cwd=repo,
                          capture_output=True, timeout=120).returncode:
            return None
        m = subprocess.run(["git", *_identity(cwd), "merge", "--no-commit", "--no-ff", "-q", head], cwd=cwd,
                           capture_output=True, text=True, timeout=120)
        if m.returncode == 0:
            return []
        files = worktree._git(cwd, "diff", "--name-only", "--diff-filter=U", check=False).split()
        return files or None
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(cwd)], cwd=repo, capture_output=True, timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)


def route(repo: Path, cfg: dict, task: dict, head: str, onto: str = "") -> dict:
    """Where change `head` of `task` goes: {"shared": True, "branch": <integration branch>} when it merges
    cleanly there, else {"shared": False, "why": ..., "files": [...]}: a `no_push:` line in its spec, a real
    conflict (the files), or a merge that could not be checked (kept apart, the safe side). `onto` is the
    ref the integration branch is read from (default: the branch itself, else its remote-tracking ref)."""
    branch = integration_branch(cfg)
    if _NO_PUSH.search(task.get("spec") or ""):
        return {"shared": False, "branch": branch, "why": "no_push", "files": []}
    # As push reads delivery.push_branch: a local branch, else <remote>/<rest> naming a known remote's
    # tracking ref, else origin's.
    remote, _, rest = branch.partition("/")
    cands = [f"refs/heads/{branch}"]
    if rest and remote in (worktree._git(repo, "remote", check=False) or "").split():
        cands.append(f"refs/remotes/{remote}/{rest}")
    cands.append(f"refs/remotes/origin/{branch}")
    ref = onto or next((r for r in cands if worktree._git(
        repo, "rev-parse", "--verify", "--quiet", f"{r}^{{commit}}", check=False)), "")
    if not ref:   # no integration branch yet: the first change starts it
        return {"shared": True, "branch": branch, "files": []}
    files = conflicts(repo, ref, head)
    if files is None:
        return {"shared": False, "branch": branch, "why": "merge check failed", "files": []}
    if files:
        return {"shared": False, "branch": branch, "why": "conflict", "files": files}
    return {"shared": True, "branch": branch, "files": []}


def own_branch_label(decision: dict) -> str:
    """The label a change kept on its own branch carries: why, and the conflicting files."""
    files = ",".join(decision.get("files") or [])[:160]
    return f"{OWN_BRANCH_LABEL}{decision.get('why') or 'conflict'}" + (f" in {files}" if files else "")


def _labels(task: dict) -> list[str]:
    try:
        v = json.loads(task.get("labels") or "[]")
    except ValueError:
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


def status_lines(db, cfg: dict, repo_name: str = "", quiet: bool = False) -> list[str]:
    """Status lines for the integration branch of the project's repo: what open reviews of a shared
    branch cover and is not landed yet, and each change kept on a branch of its own with why. `quiet`:
    none when nothing is shared or kept apart (`ttp status` shows the line only then)."""
    branch = integration_branch(cfg)
    where = f"{repo_name}: " if repo_name else ""
    shared = []
    for r in db.q("SELECT * FROM tasks WHERE kind='review' AND status NOT IN ('done','failed','cancelled') "
                  "ORDER BY id"):
        ms = members(db, r)
        if len(ms) > 1:
            shared.append(f"review #{r['id']} of {ms[0].get('branch') or '?'} covers "
                          + ", ".join(f"#{m['id']}" for m in ms))
    kept = []
    for t in db.q("SELECT * FROM tasks WHERE kind='code' AND status NOT IN ('failed','cancelled') AND labels LIKE ? "
                  "ORDER BY id", (f'%"{OWN_BRANCH_LABEL}%',)):
        why = next((x[len(OWN_BRANCH_LABEL):] for x in _labels(t) if x.startswith(OWN_BRANCH_LABEL)), "")
        kept.append(f"#{t['id']} on {t.get('branch') or '?'} ({why})")
    if quiet and not shared and not kept:
        return []
    via = "push branch" if not is_pr_project(cfg) else "one draft PR"
    lines = [f"{where}integration branch {branch} ({via})"
             + (f": {'; '.join(shared)}, not landed yet" if shared else ": nothing waiting")]
    if kept:
        lines.append(f"{where}kept on their own branch: " + "; ".join(kept))
    return lines
