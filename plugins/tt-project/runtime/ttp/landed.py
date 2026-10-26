# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp landed`: whether a change is on the push branch, even after a rebase gave its commits new
shas. The push queue (and `ttp push`) rebase each approved head onto the branch tip, so a probe like
`git merge-base --is-ancestor <reviewed sha> origin/<branch>` never passes once they did.

- `landed:#<id>` (a start_when or retry_when) passes once task <id>'s landing is on the branch: the
  commit the push queue recorded for it or for a successor that carries its work on (a review, a
  review fix and its re-review, a rebase or re-done task: carriers), a successful detached push, the
  commit a task recorded as superseding it (`ttp task <project> superseded <id> <sha>`), else (once
  it is done and committed something) its branch's commits by patch.
- `git merge-base --is-ancestor <sha> <ref>`, as a whole probe (or after `git fetch [-q] <remote>
  <branch> &&`, on FETCH_HEAD or <remote>/<branch>), also passes when every commit of
  <sha> has an equivalent on <ref>: the same `git patch-id --stable` (git cherry), the same author,
  author date and subject (a rebase keeps them; a settled conflict changes the patch), or a commit
  the push queue replayed from it (its trailer).

Exit codes, as a probe's: 0 landed, 1 not yet, 2 cannot tell (a bad id, sha or ref)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import push
from .db import TERMINAL_TASK_STATES, dependency_ids, review_subject

LANDED_PROBE = re.compile(r"\s*landed:\s*#?(\d+)\s*")
ANCESTOR_PROBE = re.compile(r"\s*git\s+merge-base\s+--is-ancestor\s+([0-9a-f]{7,64})\s+([\w./@{}^~-]+)\s*")
# `git fetch [-q] <remote> <branch> && git merge-base --is-ancestor <sha> FETCH_HEAD|<remote>/<branch>`.
FETCH_ANCESTOR_PROBE = re.compile(
    r"\s*git\s+fetch(?:\s+(?:-q|--quiet))*\s+([\w.-]+)\s+([\w./-]+)\s*&&\s*"
    r"git\s+merge-base\s+--is-ancestor\s+([0-9a-f]{7,64})\s+([\w./-]+)\s*")
# `ttp landed --task <id>` / `ttp landed #<id>` inside a longer probe: the task it waits on.
_LANDED_TASK = re.compile(r"\bttp\s+landed\b[^|;&\n]*?(?:--task(?:=|\s+)|\s#)['\"]?([^\s'\"|;&)]+)")
# A literal commit sha in a probe: 7 to 40 lowercase hex characters with a digit and a letter (an
# epoch or a plain word is no sha), not part of a longer word or a dashed id (a UUID).
_LITERAL_SHA = re.compile(r"(?<![\w-])(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w-])")
# Probes whose hex is a task-landing wait, a reply hash or an upstream note id, not a deployed commit.
_SHA_WAIT_OK = re.compile(r"landed:|\bttp\s+landed\b|ack-wait|upstream\s+--fixed")
REPLAYED = "Ttp-Replayed-From"   # batch.REPLAYED: a commit the push queue settled names the one it replays
FETCH_S = 40                     # within the daemon's 60 s probe timeout
NOT_YET, CANNOT = 1, 2


def probe_command(probe: str, p=None) -> str:
    """The shell command the daemon runs for a start_when or retry_when: `landed:#<id>` and an
    ancestor probe (bare, or after a `git fetch` of the ref it reads) become `ttp landed` on the
    probe's own ref; anything else runs as written. With project `p`, a fetch probe of the push
    branch (`delivery.push_branch` on its remote) whose sha a push-queue row holds as its head waits
    on that row's task (`ttp landed --task <id>`), which also counts a landing the queue recorded.
    Known gap, accepted: a probe of any other ref (main, another branch) keeps that ref and gets no
    task-based wait, so a sha the queue rebased may never appear there; no heuristic covers it, as
    waiting on the push branch instead would pass long before the sha reaches the ref asked for."""
    if m := LANDED_PROBE.fullmatch(probe or ""):
        return f"ttp landed --task {m.group(1)}"
    if m := ANCESTOR_PROBE.fullmatch(probe or ""):
        return f"ttp landed {m.group(1)} --onto {shlex.quote(m.group(2))}"
    if not ((m := FETCH_ANCESTOR_PROBE.fullmatch(probe or ""))
            and m.group(4) in ("FETCH_HEAD", f"{m.group(1)}/{m.group(2)}")):
        return probe
    remote, branch, sha = m.group(1), m.group(2), m.group(3)
    if p is not None and _is_push_branch(p, remote, branch) and (tid := queued_task(p.db, sha)) is not None:
        return f"ttp landed --task {tid}"
    return f"ttp landed {sha} --onto {shlex.quote(f'{remote}/{branch}')}"


def _is_push_branch(p, remote: str, branch: str) -> bool:
    """Whether <remote>/<branch> is the project's push branch; False when none is configured."""
    try:
        return push.target(p, p.root) == (remote, branch)
    except Exception:
        return False


def queued_task(db, sha: str) -> int | None:
    """The task a push-queue row with head `sha` (or a prefix of it) carries: the task that owns the
    row's branch, else the row's own task (the review that approved it). None when no row has it."""
    try:
        row = db.one("SELECT task, branch FROM push_queue WHERE head LIKE ? ORDER BY id DESC LIMIT 1",
                     (sha.lower() + "%",))
        if not row:
            return None
        owner = db.one("SELECT id FROM tasks WHERE branch=? AND kind!='review' ORDER BY id LIMIT 1", (row["branch"],))
    except Exception:
        return None
    return int(owner["id"] if owner else row["task"])


def task_refs(probe: str | None) -> list[str]:
    """The task ids a start_when, retry_when or hold probe waits on: `landed:#<id>`, and every
    `ttp landed --task <id>` or `ttp landed #<id>` in it, as written (a bad one stays as it is)."""
    probe = probe or ""
    m = LANDED_PROBE.fullmatch(probe)
    return list(dict.fromkeys([m.group(1)] if m else _LANDED_TASK.findall(probe)))


def literal_sha(probe: str | None) -> str | None:
    """The first literal commit sha a probe tests (a grep of a deployed commit, say), or None. A
    `landed:#<id>` or `ttp landed` wait and an ancestor probe the daemon runs as `ttp landed`
    (probe_command) follow a rebase or replay; any other sha goes stale after one or a redeploy."""
    probe = probe or ""
    if _SHA_WAIT_OK.search(probe) or probe_command(probe) != probe:
        return None
    m = _LITERAL_SHA.search(probe)
    return m.group(0) if m else None


def missing_task_refs(db, probe: str | None) -> list[str]:
    """The ids of `probe`'s task references (task_refs) that name no task in this project: such a
    probe would wait forever. A task that exists keeps today's handling, whatever its state."""
    return [r for r in task_refs(probe) if not (r.isdigit() and int(r) > 0 and db.task(int(r)))]


def missing_words(missing: list[str]) -> str:
    """'#0' or '#0 and #7': the missing ids as the user reads them."""
    return " and ".join(f"#{r}" for r in missing)


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
    """The push branch to look on: <remote>/<branch>, or the local refs/heads/<branch> when the repo
    has no such remote (a remote-less repo pushes nowhere, so its branch is the local one)."""
    remote, branch = push.target(p, repo)
    if remote not in _git(repo, "remote").stdout.split():
        return f"refs/heads/{branch}"
    return f"{remote}/{branch}"


def record_push(p, marker: Path, m: dict) -> None:
    """Keep a successful shared-branch push independently of its run's outcome. The marker is
    written first; the daemon retries this insert on every tick, including after a restart."""
    if m.get("status") != "pushed" or m.get("own") or m.get("kind") == "batch":
        return
    task, sha, target = m.get("task"), m.get("sha"), m.get("target")
    if not (isinstance(task, (str, int)) and re.fullmatch(r"[0-9]+", str(task))
            and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40,64}", sha)
            and isinstance(target, str) and target):
        return
    if p.db.one("SELECT marker FROM task_landings WHERE marker=?", (marker.name,)):
        return
    if not p.db.task(int(task)):
        return
    ended = m.get("ended")
    ts = float(ended) if isinstance(ended, (int, float)) and not isinstance(ended, bool) else time.time()
    p.db.x("INSERT INTO task_landings(marker, task, sha, target, ts) VALUES(?,?,?,?,?) "
           "ON CONFLICT(marker) DO NOTHING", (marker.name, int(task), sha, target, ts))


def reconcile_pushes(p, tid: int) -> None:
    """A worker can finish before the daemon tends its detached pushes, regardless of its handoff."""
    for marker in sorted((p.state / push.DETACHED).glob(f"t{tid}-*.json")):
        record_push(p, marker, push._read(marker))


# A task title that redoes another task's change on a new branch: "rebase #12 onto ...", "rebased #12".
_REBASE_TITLE = re.compile(r"\brebas(?:e|ed|es|ing)\s+(?:task\s+)?#(\d+)(?!\d)", re.I)


def carriers(db, tid: int) -> dict[int, tuple[str, int | None, str]]:
    """{task: (branch, the carrier it follows, kind)} of task `tid` and every task that carries its change on: a review of it
    (review_subject), a task that `continues:` or fixes (`review_fix:`) one of them, rebases or carries
    one of their branches (`rebases:`/`pr_branch:`) or whose title rebases one ("rebased #<id>"), and
    so on down the chain (a re-review of a fix of a review of it). When the push queue lands such a
    successor, `tid`'s work landed, under other patches if a conflict was settled on the way."""
    try:
        rows = db.q("SELECT id, kind, branch, labels, title FROM tasks WHERE id>? AND (kind='review' "
                    "OR labels LIKE '%continues:%' OR labels LIKE '%review_fix:%' OR labels LIKE '%rebases:%' "
                    "OR labels LIKE '%pr_branch:%' OR labels LIKE '%auto_review:%' OR title LIKE '%rebas%' "
                    "OR title LIKE '%review%') ORDER BY id", (tid,))
    except Exception:
        rows = []
    task = db.task(tid)
    out = {tid: ((task or {}).get("branch") or "", None, (task or {}).get("kind") or "")}
    links = []
    for r in rows:
        try:
            labels = json.loads(r["labels"] or "[]")
        except (ValueError, TypeError):
            labels = []
        labels = [lb for lb in labels if isinstance(lb, str)] if isinstance(labels, list) else []
        ids = {int(lb.partition(":")[2]) for lb in labels if lb.startswith(("continues:", "review_fix:"))
               and lb.partition(":")[2].isdigit()}
        if (s := review_subject({"labels": r["labels"], "title": r["title"]})) is not None:
            ids.add(s)
        ids |= {int(x) for x in _REBASE_TITLE.findall(r["title"] or "")}
        names = {lb.partition(":")[2] for lb in labels if lb.startswith(("rebases:", "pr_branch:"))} - {""}
        links.append((r["id"], r["branch"] or "", r["kind"] or "", ids, names))
    grew = True
    while grew:   # a successor always has a later id, but a link may name a branch rather than an id
        grew = False
        branches = {v[0]: k for k, v in out.items() if v[0]}
        for i, b, kind, ids, names in links:
            on = max(ids & out.keys(), default=None) or branches.get(min(names & branches.keys(), default=""))
            if i not in out and on is not None:
                out[i], grew = (b, on, kind), True
    return out


def recorded(db, tid: int, carry: dict | None = None) -> tuple[list[str], str]:
    """The landed commits saved for task `tid` or a task that carries it on (`carry`, else
    carriers): a push queue row it or its branch landed in, a successful detached push, a recorded
    supersession; ([], "") when none. `ttp landed --task` and deadwait both judge from this."""
    carry = carriers(db, tid) if carry is None else carry
    branches = {v[0] for v in carry.values() if v[0]}

    def via(src: str, i) -> str:
        """`src`, naming the successor it came through (not `tid` itself or a plain review of it)."""
        direct = i == tid or i not in carry or (carry[i][1] == tid and carry[i][2] == "review")
        return src if direct else f"{src} through #{i}"
    try:
        rows = db.q("SELECT q.task, q.branch, q.landed_sha, q.pushed_sha FROM push_queue q "
                    "WHERE q.status IN ('pushed','landed') ORDER BY q.id")
    except Exception:
        rows = []
    hits = [r for r in rows if (r["landed_sha"] or r["pushed_sha"]) and (r["task"] in carry or r["branch"] in branches)]
    if hits:
        return [r["landed_sha"] or r["pushed_sha"] for r in hits], via("its push queue landing", hits[-1]["task"])
    rows = db.q("SELECT task, sha FROM task_landings ORDER BY marker")
    hits = [r for r in rows if r["task"] in carry]
    if hits:
        return list(dict.fromkeys(r["sha"] for r in hits)), via("its detached push landing", hits[-1]["task"])
    rows = db.q("SELECT task, sha FROM task_superseded WHERE task IN (%s) ORDER BY ts" % ",".join("?" * len(carry)),
                list(carry))
    if rows:
        return [r["sha"] for r in rows], via("the commit recorded as superseding it", rows[-1]["task"])
    return [], ""


def task_shas(p, tid: int) -> tuple[list[str], str]:
    """The commits that carry task `tid` to the branch, and where they come from: the landed commits
    the push queue recorded for it or a task that carries it on (carriers: a review of it, a fix, a
    re-review, a rebase or re-done task, or any of their branches), then their successful detached
    pushes or recorded supersessions (recorded), else, once it is done with commits of its own, its
    branch's head. ([], why) when there is none yet."""
    db = p.db
    task = db.task(tid)
    if not task:
        return [], f"no task #{tid}"
    branch = task.get("branch") or ""
    shas, why = recorded(db, tid)
    if shas:
        return shas, why
    head = _commit(p.root, f"refs/heads/{branch}") if branch else ""
    if not head:
        return [], f"#{tid} has no landing recorded and no branch"
    # The daemon makes the branch at the push branch's tip when the task starts: until the task is
    # done and has commits of its own, its head is "on the branch" with nothing landed.
    if task.get("status") != "done":
        return [], f"#{tid} has no landing recorded and has not finished ({task.get('status')})"
    if head == _start(p.root, branch):
        return [], f"#{tid} has no landing recorded and no commits on its branch {branch}"
    return [head], f"its branch {branch}"


def _task_repos(p, tid: int) -> list[Path]:
    """The repositories other than the project root's that task `tid` worked in, newest run first:
    each run's recorded working directory (run.json cwd), or the nearest folder above it once that
    worktree is gone. One that shares the project root's git directory is left out (it is the root)."""
    def common(path: Path) -> str:
        out = _git(path, "rev-parse", "--git-common-dir")
        return str((path / out.stdout.strip()).resolve()) if out.returncode == 0 and out.stdout.strip() else ""
    seen, out = {common(p.root)} - {""}, []
    for r in p.db.q("SELECT id, dir FROM runs WHERE task=? ORDER BY id DESC", (tid,)):
        try:
            cwd = json.loads(((Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])) / "run.json").read_text())["cwd"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        path = Path(str(cwd or "")).expanduser()
        if not path.is_absolute():
            continue
        while not path.is_dir() and path != path.parent:
            path = path.parent
        key = common(path)
        if key and key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _run_dir_repos(p, tid: int) -> list[Path]:
    """The repositories of the linked worktrees workers made in run directories (runs/<id>/<name>/.git),
    task `tid`'s runs first, then the rest of the project's; the project root's is left out."""
    from .worktree import _repo_of, run_worktree_dirs
    own = {str((Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])).resolve())
           for r in p.db.q("SELECT id, dir FROM runs WHERE task=?", (tid,))}
    dirs = sorted(run_worktree_dirs(p), key=lambda d: str(d.parent.resolve()) not in own)
    root = p.root.resolve()
    return list(dict.fromkeys(r for r in map(_repo_of, dirs) if r is not None and r.resolve() != root))


def record_superseded(p, tid: int, sha: str, ref: str | None = None, repo: str | None = None) -> str:
    """Record that done task `tid`'s work landed as `sha` (re-landed under another commit, e.g. after
    a push conflict), so the local-only check stops flagging its branch at the head it has now. The
    branch and `sha` are read in the repository the task worked in (its runs' working directories),
    else the project root. Refused unless the task is done and `sha` is on `ref` (default: the push
    branch in the project root; any other ref of a second repository), came after the branch left it
    (not the branch's base or an older commit) and changes a file the branch changes; ValueError says
    why. A task with no branch on record needs `ref`: when it names a branch (looked up also in the
    repositories of its runs' worktrees) that does not hold `sha`, that branch is the task's work in a
    second repository; else only `sha` on `ref` is needed (_record_branchless). `repo`, a path, is searched
    first, before the task's own repositories and the project root.

    A code task in 'review' is accepted on the same checks (its work landed), a harness task in 'review'
    or done (with no `ref` or `repo`) when `sha` is merged into the harness checkout's HEAD and its
    worktree branch has nothing unmerged (_record_harness); one left in 'review' is then closed as done."""
    task = p.db.task(tid)
    if not task:
        raise ValueError(f"no task #{tid}")
    status, kind = task.get("status"), task.get("kind")
    if status != "done" and not (status == "review" and kind in ("code", "harness")):
        raise ValueError(f"not recorded: task #{tid} is {status}, not done"
                         + (" or a code or harness task in review" if status == "review" else ""))
    if kind == "harness" and not ref and not repo:
        words = _record_harness(p, task, sha)
    else:
        words = _record_work(p, task, sha, ref, repo)
    if status == "review":
        full = p.db.one("SELECT sha FROM task_superseded WHERE task=? ORDER BY ts DESC LIMIT 1", (tid,))["sha"]
        words += _close_review(p, task, full)
    if abandoned(p.db, tid):   # its work landed after all
        p.db.x("DELETE FROM task_abandoned WHERE task=?", (tid,))
        words += "; its abandon record is cleared"
    return words


def _record_harness(p, task: dict, sha: str) -> str:
    """record_superseded for a harness task: `sha` is a commit of the harness checkout, in its HEAD,
    and the task's worktree branch has no commit HEAD lacks."""
    from . import harnesstree
    tid, h = task["id"], p.harness
    if not (h / ".git").exists():
        raise ValueError(f"not recorded: the harness {h} is not a git repository")
    full = _commit(h, sha)
    if not full:
        raise ValueError(f"no commit {sha} in the harness {h}")
    if _git(h, "merge-base", "--is-ancestor", full, "HEAD").returncode != 0:
        raise ValueError(f"not recorded: {full[:10]} is not merged into the harness (not in its HEAD)")
    if harnesstree.unmerged(p, tid):
        raise ValueError(f"not recorded: branch {harnesstree.branch(tid)} of task #{tid} has commits not "
                         f"merged into the harness")
    p.db.x("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
           (tid, "", full, time.time()))
    return f"task #{tid}: recorded as superseded by {full[:10]} (merged into the harness)"


def _close_review(p, task: dict, sha: str) -> str:
    """Close `task`, left in 'review', as done now that `sha` carries its work, with the chat reply
    (pushq._reply) when this closed it: the words to add."""
    from . import pushq
    from .db import dump_result, load_result
    now, line = time.time(), f"its work is in {sha[:10]}, recorded with `ttp task superseded`"
    with p.db.tx():
        prev = load_result((p.db.task(task["id"]) or task).get("result"))
        result = {**prev, "status": "done", "superseded_by": sha,
                  "summary": f"{str(prev.get('summary') or '').rstrip()} ({line})".strip()[:1500]}
        if not p.db.conn.execute("UPDATE tasks SET status='done', blocked_reason=NULL, result=?, updated=? "
                                 "WHERE id=? AND status='review'",
                                 (dump_result(result), now, task["id"])).rowcount:
            return ""
        p.db.conn.execute("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                          (now, f"task:{task['id']}", "task_superseded", "normal",
                           f"#{task['id']} {task['title']}: {line}; closed as done.", "handled", task["id"]))
        pushq._reply(p.db, task, "done", pushq._done_text(prev, result["summary"], line))
    return "; it was in review and is now done"


def _record_work(p, task: dict, sha: str, ref: str | None, repo: str | None) -> str:
    """record_superseded for a task's branch, or with `ref` for one that never had a branch."""
    tid = task["id"]
    branch = task.get("branch") or ""
    repos = _task_repos(p, tid)
    if repo:
        rp = Path(repo).expanduser()
        if not (rp / ".git").exists() and _git(rp, "rev-parse", "--git-dir").returncode != 0:
            raise ValueError(f"--repo {repo} is not a git repository")
        repos = [rp, *repos]
    top = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if top.returncode == 0 and top.stdout.strip():   # the repository `ttp` runs in comes after --repo
        n = 1 if repo else 0
        repos = [*repos[:n], Path(top.stdout.strip()), *repos[n:]]
    if not branch:
        # A task whose work sits in a worktree of a second repository it made itself has no branch
        # on record: --ref names that branch, then read in each repository its runs touched. A task
        # that never had a branch at all: --ref is a ref that already holds the commit.
        if not ref:
            raise ValueError(f"task #{tid} has no branch on record; name the branch that holds its work, "
                             "or a ref its commit is on, with --ref <branch>")
        extra = _run_dir_repos(p, tid)
        heads = [(r, h) for r in dict.fromkeys([*repos, *extra, p.root]) if (h := _commit(r, f"refs/heads/{ref}"))]
        if not heads or any(_git(r, "merge-base", "--is-ancestor", sha, h).returncode == 0 for r, h in heads):
            return _record_branchless(p, tid, sha, ref, [*repos, p.root, *extra])
        branch, ref = ref, None
        repos += extra
    checked, first = [], None
    for repo in dict.fromkeys([*repos, p.root]):
        head = _commit(repo, f"refs/heads/{branch}")
        if head and _commit(repo, sha):
            break
        if head and not first:
            first = (repo, head)
        checked.append(str(repo))
    else:
        if not first:
            raise ValueError(f"task #{tid} has no branch {branch} to record (checked {', '.join(checked)})")
        repo, head = first   # the branch is here; the commit is in no repository searched
    full = _commit(repo, sha)
    if not full:
        raise ValueError(f"no commit {sha} in {repo}, the repository with branch {branch} "
                         f"(searched {', '.join(map(str, dict.fromkeys([*repos, p.root])))}; "
                         "run from the repository that has it, or pass --repo <path>)")
    if repo == p.root or ref:
        onto = ref or _push_ref(p, repo)
        rc, words = check(p, repo, full, onto=onto)
        if rc != 0:
            raise ValueError(f"not recorded: {words} in {repo}")
        tip = _tip(repo, onto, False)[0]
    else:
        holders = [h for h in _git(repo, "for-each-ref", "--contains", full, "--format=%(refname:short)",
                                   "refs/heads", "refs/remotes").stdout.split() if h != branch]
        if not holders:
            raise ValueError(f"not recorded: {full[:10]} is on no branch of {repo} but {branch}; "
                             "name the ref that holds it with --ref")
        tip, words = full, f"{full[:10]} is on {holders[0]} in {repo}"
    if _git(repo, "merge-base", "--is-ancestor", full, head).returncode == 0:
        raise ValueError(f"not recorded: {full[:10]} is already in branch {branch}, so it predates the work")
    base = _git(repo, "merge-base", head, tip).stdout.strip() if tip else ""
    if not base:
        raise ValueError(f"not recorded: branch {branch} shares no history with {ref or 'the push branch'}")
    theirs = set(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "--root", "-m",
                      "--first-parent", "-z", full).stdout.split("\0")) - {""}
    mine = set(_git(repo, "diff", "--name-only", "--no-renames", "-z", base, head).stdout.split("\0")) - {""}
    on_ref = bool(ref) and any(_commit(repo, r) and _git(repo, "merge-base", "--is-ancestor", full, r).returncode == 0
                               for r in (f"refs/heads/{ref}", f"refs/remotes/origin/{ref}", ref))
    if not on_ref and not theirs & mine:
        raise ValueError(f"not recorded: {full[:10]} changes none of the files branch {branch} changes "
                         f"(compared: the files of {full[:10]} against the files branch {branch} changed since it left "
                         f"{ref or 'the push branch'}); if the work landed earlier on a branch, pass it with "
                         "--ref <branch>: a commit already on that local or remote-tracking ref is accepted")
    p.db.x("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
           (tid, head, full, time.time()))
    return f"task #{tid}: branch {branch} at {head[:10]} recorded as superseded by {full[:10]} ({words})"


def _record_branchless(p, tid: int, sha: str, ref: str, repos: list[Path]) -> str:
    """record_superseded for done task `tid` that never had a branch: `sha` must be on `ref`, read in
    the first of `repos` that has the commit. The record has no branch head ("")."""
    repos = list(dict.fromkeys(repos))
    repo = next((r for r in repos if _commit(r, sha)), None)
    if repo is None:
        raise ValueError(f"no commit {sha} in {', '.join(map(str, repos))}")
    full = _commit(repo, sha)
    rc, words = check(p, repo, full, onto=ref)
    if rc != 0:
        raise ValueError(f"not recorded: {words} in {repo}")
    p.db.x("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
           (tid, "", full, time.time()))
    return f"task #{tid}: no branch; recorded as superseded by {full[:10]} ({words})"


def _keys(repo: Path, args: list[str], skip: list[str]) -> list[tuple[str, str]]:
    """[(commit, key)] of `git log <args>`, oldest first: the key is a hash of each file's added and
    removed lines (no context, no line numbers), with the `skip` paths left out. A rebase that moved
    the context or took the version files keeps the key; git's patch-id does not. Merges and commits
    that change only `skip` paths are left out."""
    out = _git(repo, "log", "--reverse", "--no-merges", "-p", "-U0", "--no-renames", "--no-color",
               "--format=%x00%H", *args, "--", ".", *(f":(exclude){x}" for x in skip), timeout=60)
    if out.returncode != 0:
        return []
    keys = []
    for chunk in out.stdout.split("\0")[1:]:
        sha, _, body = chunk.partition("\n")
        if key := _key_of(body):
            keys.append((sha.strip(), key))
    return keys


def _key_of(diff: str) -> str:
    """The key _keys gives a diff's changed lines; "" when it changes nothing."""
    files: dict[str, list[str]] = {}
    name, hunk = "", False
    for ln in diff.splitlines():
        if ln.startswith("diff --git "):
            name, hunk = ln, False
            files.setdefault(name, [])
        elif ln.startswith("@@"):
            hunk = True
        elif hunk and ln[:1] in ("+", "-"):
            files[name].append(ln)
    files = {f: lines for f, lines in files.items() if lines}
    if not files:
        return ""
    return hashlib.sha256("\n".join(f"{f}\n" + "\n".join(lines) for f, lines in sorted(files.items()))
                          .encode()).hexdigest()


def equivalent(p, branch: str, bump=None) -> tuple[str, str] | None:
    """(commit on the push branch, branch head) when `branch`'s change already landed there under
    other commits: each of its own commits, or its whole change at once (a squash), has the same
    changed lines as a commit the push branch gained since the two parted, the version files and
    changesets of `bump` (delivery.version_bump) aside. None when it did not, the branch already is
    on the push branch (a plain landing) or has no change of its own, or it cannot be read.
    Local refs only: no fetch."""
    repo = p.root
    head = _commit(repo, f"refs/heads/{branch}")
    try:
        tip = _commit(repo, _push_ref(p, repo))
    except ValueError:
        return None
    if not head or not tip or _git(repo, "merge-base", "--is-ancestor", head, tip).returncode == 0:
        return None
    base = _git(repo, "merge-base", tip, head).stdout.strip()
    if not base:
        return None
    try:
        cfg = push.bump_of(bump)
    except ValueError:
        cfg = None
    skip = (cfg["files"] + ([cfg["changeset_dir"]] if cfg["changeset_dir"] else [])) if cfg else []
    mine = _keys(repo, [f"{base}..{head}"], skip)
    if not mine:
        return None
    theirs: dict[str, str] = {}
    for sha, key in _keys(repo, ["--max-count=500", f"{base}..{tip}"], skip):
        theirs.setdefault(key, sha)
    if all(key in theirs for _, key in mine):
        return theirs[mine[-1][1]], head
    whole = _git(repo, "diff", "-U0", "--no-renames", "--no-color", base, head, "--", ".",
                 *(f":(exclude){x}" for x in skip), timeout=60)
    key = _key_of(whole.stdout) if whole.returncode == 0 else ""
    return (theirs[key], head) if key in theirs else None


def on_push_branch(p, branch: str) -> tuple[str, str] | None:
    """(commit on the push branch, branch head) when `branch` already is on the push branch as it
    stands (a plain landing, which equivalent leaves out): its head is an ancestor of the tip (the
    commit is its head), or each of its commits has an equivalent there (on_branch: patch-id, the same
    author, date and subject, or a replay trailer; the commit is the match of its top one by author,
    date and subject, else the tip). None when it is not, it has no commit of its own (its head is
    where it was created), or it cannot be read. Local refs only: no fetch."""
    repo = p.root
    head = _commit(repo, f"refs/heads/{branch}")
    try:
        tip = _commit(repo, _push_ref(p, repo))
    except ValueError:
        return None
    if not head or not tip or head == _start(repo, branch):
        return None
    if _git(repo, "merge-base", "--is-ancestor", head, tip).returncode == 0:
        return head, head
    if not on_branch(repo, head, tip):
        return None
    base = _git(repo, "merge-base", tip, head).stdout.strip()
    fmt = "--format=%H%x00%an%x00%ae%x00%at%x00%s"
    top = _key(_git(repo, "log", "-1", fmt, head).stdout.strip())
    for ln in _git(repo, "log", "--no-merges", fmt, f"{base}..{tip}").stdout.splitlines():
        if top and (k := _key(ln)) and k[1] == top[1]:
            return k[0], head
    return tip, head


def superseded(db, tids) -> dict[int, list[tuple[str, str]]]:
    """{task: [(recorded head, sha)]} of record_superseded for tasks `tids`."""
    tids = [int(t) for t in tids]
    out: dict[int, list[tuple[str, str]]] = {}
    if tids:
        for r in db.q("SELECT task, head, sha FROM task_superseded WHERE task IN (%s)" % ",".join("?" * len(tids)),
                      tids):
            out.setdefault(r["task"], []).append((r["head"], r["sha"]))
    return out


def superseded_rows(p, tid: int) -> list[dict]:
    """The `ttp task superseded` records of task `tid`, oldest first: sha, head, ts and `on_branch`
    (True/False: that commit is an ancestor of the push branch's local tip; None when it cannot be read)."""
    out = []
    for r in p.db.q("SELECT head, sha, ts FROM task_superseded WHERE task=? ORDER BY ts", (int(tid),)):
        on = None
        try:
            tip = _commit(p.root, _push_ref(p, p.root))
            if tip:
                on = _git(p.root, "merge-base", "--is-ancestor", r["sha"], tip).returncode == 0
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
        out.append({"sha": r["sha"], "head": r["head"], "ts": r["ts"], "on_push_branch": on})
    return out


def abandoned(db, tid: int) -> tuple[str, float] | None:
    """(why, when) of `ttp task abandon` for task `tid`, None when it was not abandoned."""
    r = db.one("SELECT why, ts FROM task_abandoned WHERE task=?", (int(tid),))
    return (r["why"], r["ts"]) if r else None


def abandon(p, tid: int, why: str) -> str:
    """Record that done code task `tid`'s work will never land (its approach was dropped), with `why`
    and the time; its branch is kept. The local-only check and its status line, the push audit and the
    landed-wait checks then leave it out, and `landed:#<id>` answers CANNOT naming `why`, so a task
    waiting on it is raised as a broken probe. ValueError for no reason, a task that is not done,
    another kind (a harness task's work lands by merging into the harness, not on the push branch), one
    already abandoned, one whose landing is recorded or on the push branch, and one whose work can still
    land: an open task that carries it on (its review, a fix or continuation: carriers, or an open review
    that depends on it or names its branch, as the daemon's auto-review finds one) or a push queue
    entry of one of them or its branch still waiting to push."""
    why, task = " ".join((why or "").split()), p.db.task(tid)
    if not task:
        raise ValueError(f"no task #{tid}")
    if not why:
        raise ValueError(f'abandon needs a reason: ttp task {p.name} abandon {tid} "<why>"')
    if task.get("kind") != "code":
        raise ValueError(f"not abandoned: task #{tid} is a {task.get('kind')} task, not code")
    if task.get("status") != "done":
        raise ValueError(f"not abandoned: task #{tid} is {task.get('status')}, not done (cancel an open task)")
    if prev := abandoned(p.db, tid):
        raise ValueError(f"task #{tid} is already abandoned ({prev[0]})")
    carry = carriers(p.db, tid)
    shas, src = recorded(p.db, tid, carry)
    rc, words = (0, f"{src}, {shas[0][:10]}") if shas else check(p, p.root, tid=tid)
    if rc == 0:
        raise ValueError(f"not abandoned: task #{tid} has landed ({words})")
    from .worktree import named_refs
    ids, branches = sorted(carry), sorted({v[0] for v in carry.values() if v[0]})
    marks, branch = ",".join("?" * len(ids)), task.get("branch")
    for q in p.db.q(f"SELECT id, task, status FROM push_queue WHERE status IN ('approved','held','batched') AND "
                    f"(task IN ({marks}) OR branch IN ({','.join('?' * len(branches)) or 'NULL'})) ORDER BY id",
                    (*ids, *branches)):
        raise ValueError(f"not abandoned: task #{tid} is queued to push (entry #{q['id']} of task #{q['task']}, "
                         f"{q['status']}): cancel that first")
    for t in p.db.q(f"SELECT * FROM tasks WHERE id!=? AND status NOT IN ({','.join('?' * len(TERMINAL_TASK_STATES))}) "
                    f"AND (id IN ({marks}) OR kind='review') ORDER BY id", (tid, *TERMINAL_TASK_STATES, *ids)):
        # an open review that covers it as the daemon's auto-review reads it: by dependency or a named branch
        if t["id"] in carry or tid in dependency_ids(t) or (branch and branch in named_refs(p, t)):
            raise ValueError(f"not abandoned: task #{tid}'s work can still land: {t['kind']} task #{t['id']} "
                             f"that carries it is {t['status']}: cancel that first")
    p.db.x("INSERT INTO task_abandoned(task, why, ts) VALUES(?,?,?)", (tid, why, time.time()))
    return f"task #{tid}: abandoned ({why}); its branch {task.get('branch') or '(none)'} is kept"


def landed_probe(p) -> str:
    """`delivery.landed_probe`: a read-only shell command template, `{id}` the task id (exit 0 landed,
    1 not yet, any other exit broken), that answers `landed:#<id>` in a project with no
    `delivery.push_branch`; "" when unset. Ignored while a push branch is set."""
    return str((p.config().get("delivery") or {}).get("landed_probe") or "").strip()


def _has_push_branch(p) -> bool:
    return bool(str((p.config().get("delivery") or {}).get("push_branch") or "").strip())


def can_check(p) -> bool:
    """Whether a `landed:#<id>` wait can ever pass here: a push branch, else a landed probe. Without
    either the harness chains with depends_on, the coordinator's task_add/task_update turn `landed:#<id>`
    into depends_on, `ttp task` refuses it (NO_CHECK), and the daemon's probes (DONE_ENV) wait on the task being done."""
    return _has_push_branch(p) or bool(landed_probe(p))


NO_CHECK = ("`landed:#<id>` cannot be checked in this project: it sets neither delivery.push_branch nor "
            "delivery.landed_probe. Wait on the task with depends_on instead, or set delivery.landed_probe "
            "(a read-only command with {id} for the task id: exit 0 landed, 1 not yet)")


# Set in the probes the daemon runs (Daemon._probe_env): there a `landed:#<id>` this project cannot
# check (can_check) waits on task <id> being done, as depends_on would, never exit 2 into a broken
# probe, however the probe was written or stored. `ttp landed` run by hand keeps CANNOT and NO_CHECK.
DONE_ENV = "TTP_LANDED_OR_DONE"
# One `&&` term that waits on a task's landing on the push branch (no --onto): `ttp landed --task <id>`
# or `ttp landed #<id>`, quoted or not.
_TASK_TERM = re.compile(r"\s*ttp\s+landed\s+(?:--task(?:=|\s+)(['\"]?)#?(?P<a>\d+)\1|(['\"]?)#(?P<b>\d+)\3)\s*")


def split_landed(probe: str | None) -> tuple[list[int], str]:
    """(the task ids, the rest) of a probe whose landing waits can be taken out as depends_on: all of
    `landed:#<id>`, or each `ttp landed --task <id>` / `ttp landed #<id>` term of a plain `&&` chain,
    the other terms kept in order. A probe with `||`, `;`, a pipe, a newline or a background `&`
    (also a redirect such as `2>&1`), a command substitution or a quote spanning an `&&` keeps its
    terms (([], probe)): only a plain `&&` chain means each term must pass."""
    probe = probe or ""
    if m := LANDED_PROBE.fullmatch(probe):
        return [int(m.group(1))], ""
    if re.search(r"[|;\n`]|\$\(|(?<!&)&(?!&)", probe):
        return [], probe
    ids, rest = [], []
    for part in probe.split("&&"):
        if m := _TASK_TERM.fullmatch(part):
            ids.append(int(m["a"] or m["b"]))
        else:
            rest.append(part.strip())
    if not ids or not all(rest) or any(len(re.findall(r"(?<!\\)['\"]", x)) % 2 for x in rest):
        return [], probe   # an empty term, or a quote that spans an `&&`: not a plain chain
    return list(dict.fromkeys(ids)), " && ".join(rest)


def _done_wait(p, tid: int) -> tuple[int, str]:
    """DONE_ENV's answer for task `tid` here: 0 once it is done, else NOT_YET with its status."""
    status = (p.db.task(tid) or {}).get("status")
    why = "no push branch or landed probe here, so its end stands for its landing, as with depends_on"
    if status == "done":
        return 0, f"#{tid} is done ({why})"
    return NOT_YET, f"#{tid} is {status}, not done yet ({why})"


def _run_landed_probe(p, cmd: str, tid: int) -> tuple[int, str]:
    """delivery.landed_probe for task `tid`, run in the project root within FETCH_S: its exit 0 and 1
    as they are, anything else (a timeout, a missing command) CANNOT naming the key."""
    from . import boundedchild
    res = boundedchild.run(cmd.replace("{id}", str(int(tid))), FETCH_S, shell=True, cwd=str(p.root), stderr="stdout")
    if res.timed_out:
        return CANNOT, f"delivery.landed_probe for #{tid} ran over {FETCH_S} s"
    if res.rc is None:
        return CANNOT, f"delivery.landed_probe {res.error}"
    out = res.out
    tail = " ".join((out or "").strip().splitlines()[-1:])[:200]
    if res.rc == 0:
        return 0, f"#{tid} landed (delivery.landed_probe)" + (f": {tail}" if tail else "")
    if res.rc == NOT_YET:
        return NOT_YET, f"#{tid} has not landed yet (delivery.landed_probe)" + (f": {tail}" if tail else "")
    return CANNOT, f"delivery.landed_probe for #{tid} exited {res.rc}" + (f": {tail}" if tail else "")


def _start(repo: Path, branch: str) -> str:
    """The commit `branch` was created at (its oldest reflog entry), "" when git kept no reflog."""
    log = _git(repo, "reflog", "show", "--format=%H", f"refs/heads/{branch}").stdout.split()
    return log[-1] if log else ""


def check(p, repo: Path, sha: str | None = None, tid: int | None = None, onto: str | None = None,
          fetch: bool = True) -> tuple[int, str]:
    """(exit code, words) for `ttp landed`: a sha, or task `tid`'s landing, on `onto` (default the
    push branch). Reads the local ref first and fetches only when that says not yet (never with
    `fetch` False: local refs only). With no push branch, a task's landing is delivery.landed_probe's
    answer (with `fetch` False it is not run: NOT_YET), and without that key CANNOT naming the fix."""
    if tid is not None and onto is None and not _has_push_branch(p):
        if not p.db.task(tid):
            return CANNOT, f"no task #{tid} in this project"
        if not (cmd := landed_probe(p)):
            return _done_wait(p, tid) if os.environ.get(DONE_ENV) else (CANNOT, NO_CHECK)
        if not fetch:   # local checks only: a project's own command may reach the network
            return NOT_YET, f"#{tid}: delivery.landed_probe not run (local check only)"
        return _run_landed_probe(p, cmd, tid)
    try:
        onto = onto or _push_ref(p, repo)
    except ValueError as e:
        return CANNOT, str(e)
    if tid is not None:
        if not p.db.task(tid):   # it would never land: a probe on it must say so, not wait forever
            return CANNOT, f"no task #{tid} in this project"
        if gone := abandoned(p.db, tid):   # a landing recorded since (a later carrier pushed it) still wins
            shas, src = recorded(p.db, tid)
            if not shas:
                return CANNOT, f"#{tid} was abandoned, its work will never land ({gone[0]}): re-point what waits on it"
        else:
            shas, src = task_shas(p, tid)
        if not shas:
            return NOT_YET, src
        what = f"#{tid} ({src})"
    else:
        full = _commit(repo, sha or "")
        if not full:
            return CANNOT, f"no commit {sha} in {repo}"
        shas, what = [full], (sha or "")[:10]
    for fetching in (False, True) if fetch else (False,):
        tip, ref = _tip(repo, onto, fetching)
        if not tip:
            if fetching:
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
