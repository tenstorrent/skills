# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp landed`: whether a change is on the push branch, even after a rebase gave its commits new
shas. The push queue (and `ttp push`) rebase each approved head onto the branch tip, so a probe like
`git merge-base --is-ancestor <reviewed sha> origin/<branch>` never passes once they did.

- `landed:#<id>` (a start_when or retry_when) passes once task <id>'s landing is on the branch: the
  commit the push queue recorded for it (push_queue.landed_sha), else its branch's commits by patch.
- `git merge-base --is-ancestor <sha> <ref>`, as a whole probe, also passes when every commit of
  <sha> has an equivalent on <ref>: the same `git patch-id --stable` (git cherry), the same author,
  author date and subject (a rebase keeps them; a settled conflict changes the patch), or a commit
  the push queue replayed from it (its trailer).

Exit codes, as a probe's: 0 landed, 1 not yet, 2 cannot tell (a bad id, sha or ref)."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from . import push
from .db import review_subject

LANDED_PROBE = re.compile(r"\s*landed:\s*#?(\d+)\s*")
ANCESTOR_PROBE = re.compile(r"\s*git\s+merge-base\s+--is-ancestor\s+([0-9a-f]{7,64})\s+([\w./@{}^~-]+)\s*")
REPLAYED = "Ttp-Replayed-From"   # batch.REPLAYED: a commit the push queue settled names the one it replays
FETCH_S = 40                     # within the daemon's 60 s probe timeout
NOT_YET, CANNOT = 1, 2


def probe_command(probe: str) -> str:
    """The shell command the daemon runs for a start_when or retry_when: `landed:#<id>` and a bare
    ancestor probe become `ttp landed`; anything else runs as written."""
    if m := LANDED_PROBE.fullmatch(probe or ""):
        return f"ttp landed --task {m.group(1)}"
    if m := ANCESTOR_PROBE.fullmatch(probe or ""):
        return f"ttp landed {m.group(1)} --onto {shlex.quote(m.group(2))}"
    return probe


def _git(repo: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _commit(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").stdout.strip()


def _key(line: str) -> tuple[str, str] | None:
    """(sha, author name/email/date/subject) of a `%H%x00%an%x00%ae%x00%at%x00%s` log line."""
    parts = line.split("\0")
    return (parts[0], "\0".join(parts[1:])) if len(parts) == 5 else None


def on_branch(repo: Path, sha: str, tip: str) -> bool:
    """Every commit of `sha` is on `tip`: `sha` is an ancestor, or each of its commits not on `tip`
    has an equivalent there (git cherry's patch-id, the same author, date and subject, or a replay
    trailer naming it). Version-bump commits on its top are left out, as the push queue drops them."""
    if _git(repo, "merge-base", "--is-ancestor", sha, tip).returncode == 0:
        return True
    base = _git(repo, "merge-base", tip, sha).stdout.strip()
    if not base:
        return False
    top = sha
    while top != base and re.search(rf"^{push.BUMP_TRAILER}: ", _git(repo, "log", "-1", "--format=%B", top).stdout,
                                    re.M):
        top = _commit(repo, f"{top}^")
        if not top:
            return False
    if top == base:
        return True
    left = {ln.split()[1] for ln in _git(repo, "cherry", tip, top, base).stdout.splitlines()
            if ln.startswith("+ ") and len(ln.split()) == 2}
    if not left:
        return True
    fmt = "--format=%H%x00%an%x00%ae%x00%at%x00%s"
    mine = {k[0]: k[1] for ln in _git(repo, "log", "--no-merges", fmt, f"{base}..{top}").stdout.splitlines()
            if (k := _key(ln))}
    theirs = {k[1] for ln in _git(repo, "log", "--no-merges", fmt, f"{base}..{tip}").stdout.splitlines()
              if (k := _key(ln))}
    replayed = set(re.findall(rf"^{REPLAYED}: ([0-9a-f]{{40,64}})[ \t]*$",
                              _git(repo, "log", "--format=%B", f"{base}..{tip}").stdout, re.M))
    return all(c in replayed or mine.get(c) in theirs for c in left)


def _tip(repo: Path, onto: str, fetch: bool) -> tuple[str, str]:
    """(tip sha, ref name) of `onto` (a ref, remote/branch or branch of the repo's remotes), fetched
    first when asked and it names a remote branch. ("", why) when it cannot be read."""
    remotes = _git(repo, "remote").stdout.split()
    remote, _, branch = onto.partition("/")
    if onto.startswith("refs/remotes/"):
        remote, _, branch = onto[len("refs/remotes/"):].partition("/")
    if fetch and remote in remotes and branch:
        try:
            subprocess.run(["git", "-C", str(repo), "fetch", "-q", remote,
                            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=FETCH_S,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except subprocess.TimeoutExpired:
            pass
    tip = _commit(repo, onto)
    return (tip, onto) if tip else ("", f"cannot read {onto}")


def _push_ref(p, repo: Path) -> str:
    remote, branch = push.target(p, repo)
    return f"{remote}/{branch}"


def task_shas(p, tid: int) -> tuple[list[str], str]:
    """The commits that carry task `tid` to the branch, and where they come from: the landed commits
    the push queue recorded for it (its own approvals, those of a review of it, or of its branch),
    else its branch's head. ([], why) when there is none yet."""
    db = p.db
    task = db.task(tid)
    if not task:
        return [], f"no task #{tid}"
    branch = task.get("branch") or ""
    try:
        rows = db.q("SELECT q.task, q.branch, q.landed_sha, q.pushed_sha, t.labels, t.title FROM push_queue q "
                    "LEFT JOIN tasks t ON t.id=q.task WHERE q.status IN ('pushed','landed') ORDER BY q.id")
    except Exception:
        rows = []
    shas = [r["landed_sha"] or r["pushed_sha"] for r in rows
            if (r["landed_sha"] or r["pushed_sha"]) and (
                r["task"] == tid or (branch and r["branch"] == branch)
                or review_subject({"labels": r["labels"], "title": r["title"]}) == tid)]
    if shas:
        return shas, "its push queue landing"
    head = _commit(p.root, f"refs/heads/{branch}") if branch else ""
    if head:
        return [head], f"its branch {branch}"
    return [], f"#{tid} has no landing recorded and no branch"


def check(p, repo: Path, sha: str | None = None, tid: int | None = None, onto: str | None = None) -> tuple[int, str]:
    """(exit code, words) for `ttp landed`: a sha, or task `tid`'s landing, on `onto` (default the
    push branch). Reads the local ref first and fetches only when that says not yet."""
    try:
        onto = onto or _push_ref(p, repo)
    except ValueError as e:
        return CANNOT, str(e)
    if tid is not None:
        shas, src = task_shas(p, tid)
        if not shas:
            return NOT_YET, src
        what = f"#{tid} ({src})"
    else:
        full = _commit(repo, sha or "")
        if not full:
            return CANNOT, f"no commit {sha} in {repo}"
        shas, what = [full], (sha or "")[:10]
    for fetch in (False, True):
        tip, ref = _tip(repo, onto, fetch)
        if not tip:
            if fetch:
                return CANNOT, ref
            continue
        if any(_commit(repo, s) and on_branch(repo, s, tip) for s in shas):
            return 0, f"{what} is on {ref}"
    return NOT_YET, f"{what} is not on {onto} yet"


def run(p, args) -> int:
    tid = args.task
    if tid is None and args.ref and re.fullmatch(r"#\d+", args.ref):
        tid = int(args.ref[1:])
    if tid is None and not args.ref:
        print("ttp landed: give a commit or --task <id>")
        return CANNOT
    rc, words = check(p, p.root, None if tid is not None else args.ref, tid, args.onto)
    print(words)
    return rc
