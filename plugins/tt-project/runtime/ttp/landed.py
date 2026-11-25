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

from . import gitcache, gitmemo, replymsg, repoid
from .db import TERMINAL_TASK_STATES, dependency_ids, load_result, review_subject
from .util import read_dict

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
    from . import push   # late: push loads pushq, whose imports reach this module
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
    t = gitmemo.timeout(timeout, args)
    run = lambda: gitcache.run(repo, args, lambda: gitmemo.run(   # noqa: E731
        ["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=t, stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}), t)
    return repoid.read(repo, args, run) if repoid.identity(args) else run()


def _commit(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").stdout.strip()


def _key(line: str) -> tuple[str, str] | None:
    """(sha, author name/email/date/subject) of a `%H%x00%an%x00%ae%x00%at%x00%s` log line."""
    parts = line.split("\0")
    return (parts[0], "\0".join(parts[1:])) if len(parts) == 5 else None


def on_branch(repo: Path, sha: str, tip: str) -> bool:
    """Every commit of `sha` is on `tip`: `sha` is an ancestor, or each of its commits not on `tip`
    has an equivalent there (git cherry's patch-id, the same author, date and subject, or a replay
    trailer naming it). Version-bump commits on its top are left out, as the push queue drops them.
    Commits never change under their ids, so the answer for two full ids is read once (gitcache.fixed).
    An ancestor whose change a later revert took out of `tip` (reverted_by) is not on it.
    A git read that failed answers False and keeps nothing: the next call asks again."""
    return bool(holds(repo, sha, tip))


def holds(repo: Path, sha: str, tip: str) -> bool | None:
    """on_branch's answer, None when a git read failed (cannot tell): for a caller that marks a claim
    unverified rather than false."""
    return gitcache.fixed(repo, "on_branch", sha, tip, lambda: _on_branch(repo, sha, tip))


def _on_branch(repo: Path, sha: str, tip: str) -> bool | None:
    """on_branch's answer; None when a git read failed (cannot tell)."""
    from . import push   # late: push loads pushq, whose imports reach this module
    def out(*args: str, ok: tuple[int, ...] = (0,)) -> str | None:
        r = _git(repo, *args)
        return r.stdout if r.returncode in ok else None

    anc = _git(repo, "merge-base", "--is-ancestor", sha, tip).returncode
    if anc == 0:   # an ancestor counts while a later revert has not taken its change out of `tip`
        gone = reverted_by(repo, sha, tip)
        return None if gone is None else not gone
    if anc != 1:
        return None
    base = out("merge-base", tip, sha, ok=(0, 1))     # 1: no common ancestor
    if base is None:
        return None
    base = base.strip()
    if not base:
        return False
    top = sha
    while top != base:
        body = out("log", "-1", "--format=%B", top)
        if body is None:
            return None
        if not re.search(rf"^{push.BUMP_TRAILER}: ", body, re.M):
            break
        parent = out("rev-parse", "--verify", "--quiet", f"{top}^^{{commit}}", ok=(0, 1))
        if parent is None:
            return None
        top = parent.strip()
        if not top:
            return False
    if top == base:
        return True
    cherry = out("cherry", tip, top, base)
    if cherry is None:
        return None
    left = {ln.split()[1] for ln in cherry.splitlines() if ln.startswith("+ ") and len(ln.split()) == 2}
    if not left:
        return True
    fmt = "--format=%H%x00%an%x00%ae%x00%at%x00%s"
    logs = [out("log", "--no-merges", fmt, f"{base}..{top}"), out("log", "--no-merges", fmt, f"{base}..{tip}"),
            out("log", "--format=%B", f"{base}..{tip}")]
    if None in logs:
        return None
    mine = {k[0]: k[1] for ln in logs[0].splitlines() if (k := _key(ln))}
    theirs = {k[1] for ln in logs[1].splitlines() if (k := _key(ln))}
    replayed = set(re.findall(rf"^{REPLAYED}: ([0-9a-f]{{40,64}})[ \t]*$", logs[2], re.M))
    return all(c in replayed or mine.get(c) in theirs for c in left)


# A revert's message: git revert's own line, or the push queue's range revert (postland.revert)
_REVERTS = re.compile(r"^(?:This reverts commit ([0-9a-f]{7,64})|Reverts ([0-9a-f]{7,64})\.\.([0-9a-f]{7,64}))", re.M)
_reverted: dict[tuple[str, str, str], str] = {}


def reverted_by(repo: Path, sha: str, tip: str) -> str | None:
    """The commit on `tip` that took ancestor `sha`'s change out of it, or "": a later commit (sha..tip) says
    it reverts `sha` (git revert's "This reverts commit <sha>", or the push queue's "Reverts A..B" range
    holding it) and `sha`'s diff no longer reverse-applies to `tip`'s tree (a re-applied change does). A
    later edit with no revert naming it, a merge, an empty diff or a non-ancestor reads "". None when a git
    read failed. Kept per (commit, tip): commits never change under their ids."""
    full, top = _commit(repo, sha), _commit(repo, tip)
    if not full or not top:
        return None
    key = (str(repo), full, top)
    if key not in _reverted:
        got = _reverted_by(repo, full, top)
        if got is None:
            return None
        if len(_reverted) >= 4096:
            _reverted.clear()
        _reverted[key] = got
    return _reverted[key]


def _reverted_by(repo: Path, sha: str, tip: str) -> str | None:
    if sha == tip:
        return ""
    log = _git(repo, "log", "-E", "--grep=^(This reverts commit|Reverts [0-9a-f]+\\.\\.)", "--format=%H%x00%B%x01",
               f"{sha}..{tip}", timeout=60)
    if log.returncode != 0:
        return None
    hits = []
    for rec in log.stdout.split("\x01"):
        rev, _, body = rec.strip("\n").partition("\0")
        for one, start, end in _REVERTS.findall(body):
            if one and sha.startswith(one):
                hits.append(rev)
            elif start and end:
                inside = _git(repo, "rev-list", f"{start}..{end}")
                if inside.returncode != 0:
                    return None
                if sha in inside.stdout.split():
                    hits.append(rev)
    if not hits:
        return ""
    return None if (present := _present(repo, sha, tip)) is None else ("" if present else hits[0])


def _present(repo: Path, sha: str, tip: str) -> bool | None:
    """Whether `sha`'s change is still in `tip`'s tree: its diff reverse-applies there (git apply --check -R
    on a scratch index, no checkout). True for a merge or an empty diff (ancestry decides those); None when
    git failed."""
    import tempfile
    parents = _git(repo, "rev-list", "--parents", "-n", "1", sha)
    if parents.returncode != 0:
        return None
    if len(parents.stdout.split()) > 2:
        return True
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        diff = subprocess.run(["git", "-C", str(repo), "diff-tree", "-p", "--binary", "--no-color", "--no-renames",
                               "--no-ext-diff", "--root", sha], capture_output=True, timeout=60, env=env,
                              stdin=subprocess.DEVNULL)
        if diff.returncode != 0:
            return None
        patch = diff.stdout.split(b"\n", 1)[1] if diff.stdout.startswith(sha.encode()) else diff.stdout
        if not patch.strip():
            return True
        with tempfile.TemporaryDirectory(prefix="ttp-landed-") as d:
            env["GIT_INDEX_FILE"] = str(Path(d) / "index")
            if subprocess.run(["git", "-C", str(repo), "read-tree", tip], capture_output=True, timeout=60,
                              env=env, stdin=subprocess.DEVNULL).returncode != 0:
                return None
            return subprocess.run(["git", "-C", str(repo), "apply", "--cached", "--check", "-R"], input=patch,
                                  capture_output=True, timeout=60, env=env).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def _tip(repo: Path, onto: str, fetch: bool) -> tuple[str, str]:
    """(tip sha, ref name) of `onto` (a ref, remote/branch or branch of the repo's remotes), fetched
    first when asked and it names a remote branch. ("", why) when it cannot be read."""
    remotes = _git(repo, "remote").stdout.split()
    remote, _, branch = onto.partition("/")
    if onto.startswith("refs/remotes/"):
        remote, _, branch = onto[len("refs/remotes/"):].partition("/")
    if fetch and remote in remotes and branch:
        try:
            from .ghroute import push_argv
            subprocess.run(push_argv(["git", "-C", str(repo), "fetch", "-q", remote,
                                      f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"]),
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=FETCH_S,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except subprocess.TimeoutExpired:
            pass
    tip = _commit(repo, onto)
    return (tip, onto) if tip else ("", f"cannot read {onto}")


RETIRED = "retired-push-tips.json"   # state: tips the push branch was replaced from, landed commits still on


def retired_tips(p) -> list[str]:
    """The push-branch tips the branch no longer holds: replaced non-fast-forward (a squash, a lease
    rebase: push retires the tip it replaced), or recorded by `ttp retire-tip`. A commit on one is
    landed: the branch carried it before the rewrite, though no ref points at it now."""
    return [x for x in (read_dict(p.state / RETIRED).get("tips") or []) if isinstance(x, str) and x]


def retire_tip(p, sha: str) -> str:
    """Record `sha` as a tip the push branch was replaced from (RETIRED): its commits keep counting
    as landed. Idempotent; "" or an unknown sha is not written. Returns the resolved sha, or ""
    when `sha` is no commit in the project repo (nothing was written)."""
    from .project import durable_write
    full = _commit(p.root, sha)
    if not full:
        return ""
    tips = retired_tips(p)
    if full not in tips:
        durable_write(p.state / RETIRED, json.dumps({"tips": [*tips, full]}, indent=1) + "\n")
    return full


def on_push(p, repo: Path, shas: list[str], tip: str) -> str:
    """The tip holding one of `shas`: the push branch's current `tip` or one of its retired tips
    (retired_tips), each by on_branch (an ancestor of it, or the change's equivalent); "" when none
    holds them. Where the retired-tip half of the "is this commit on the push branch" rule lives;
    landed.check keeps its own loop over the current tip, to name the ref each fetch variant read."""
    for s in shas:
        full = _commit(repo, s)
        for t in (tip, *retired_tips(p)):
            if full and t and on_branch(repo, full, t):
                return t
    return ""


def _push_ref(p, repo: Path) -> str:
    """The push branch to look on: <remote>/<branch>, or the local refs/heads/<branch> when the repo
    has no such remote (a remote-less repo pushes nowhere, so its branch is the local one)."""
    from . import push   # late: push loads pushq, whose imports reach this module
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
    from . import push   # late: push loads pushq, whose imports reach this module
    for marker in sorted((p.state / push.DETACHED).glob(f"t{tid}-*.json")):
        record_push(p, marker, read_dict(marker))


# A task title that redoes another task's change on a new branch: "rebase #12 onto ...", "rebased #12".
_REBASE_TITLE = re.compile(r"\brebas(?:e|ed|es|ing)\s+(?:task\s+)?#(\d+)(?!\d)", re.I)
_RELAND = re.compile(r"\bre-?land(?:s|ed|ing)?\s+(?:task\s+)?#(\d+)(?!\d)", re.I)


def redone(title: str | None, spec: str | None = "") -> set[int]:
    """The tasks a task re-lands by its own words: its title rebases one ("Rebase #6's integration ...")
    or its title or spec re-lands one ("re-land #6"). Their change lands only through it, so it never
    waits on their landing (task_add's overlap chain) and no second review of them is queued while
    it is open (Daemon._auto_review)."""
    return {int(x) for x in _REBASE_TITLE.findall(title or "")} \
        | {int(x) for x in _RELAND.findall(f"{title or ''}\n{spec or ''}")}


_CARRIER_SQL = ("SELECT id, kind, branch, labels, title FROM tasks WHERE id>? AND (kind='review' "
                "OR labels LIKE '%continues:%' OR labels LIKE '%review_fix:%' OR labels LIKE '%rebases:%' "
                "OR labels LIKE '%follows:%' OR labels LIKE '%relands:%' OR labels LIKE '%pr_branch:%' OR labels LIKE '%auto_review:%' OR title LIKE '%rebas%' "
                "OR title LIKE '%re-land%' OR title LIKE '%reland%' "
                "OR title LIKE '%review%') ORDER BY id")


def _links(db, after: int) -> list[tuple[int, str, str, set[int], set[str], set[int]]]:
    """(id, branch, kind, ids it carries on, branches it carries on, ids it follows) of each task after
    `after` that may carry another's change on, in id order. A `follows:` link (a follow-up of a done
    task, e.g. a third-round fix) carries only on the predecessor's own branch: carriers judges it."""
    try:
        rows = db.q(_CARRIER_SQL, (after,))
    except Exception:
        rows = []
    links = []
    for r in rows:
        try:
            labels = json.loads(r["labels"] or "[]")
        except (ValueError, TypeError):
            labels = []
        labels = [lb for lb in labels if isinstance(lb, str)] if isinstance(labels, list) else []
        ids = {int(lb.partition(":")[2]) for lb in labels if lb.startswith(("continues:", "review_fix:", "relands:"))
               and lb.partition(":")[2].isdigit()}
        if (s := review_subject({"labels": r["labels"], "title": r["title"]})) is not None:
            ids.add(s)
        ids |= redone(r["title"])
        names = {lb.partition(":")[2] for lb in labels if lb.startswith(("rebases:", "pr_branch:"))} - {""}
        follows = {int(lb[len("follows:"):]) for lb in labels if lb.startswith("follows:")
                   and lb[len("follows:"):].isdigit()}
        links.append((r["id"], r["branch"] or "", r["kind"] or "", ids, names, follows))
    return links


class Rows:
    """The saved rows carriers and recorded judge from, read once for many tasks: deadwait.check judges
    every held producer of a tick from one read. A call without one reads its own (`ttp landed --task`);
    both run the same code on the same rows."""

    def __init__(self, db, tids):
        tids = sorted({int(t) for t in tids})
        self.tasks = {t["id"]: t for t in db.q(
            f"SELECT * FROM tasks WHERE id IN ({','.join('?' * len(tids))})", tids)} if tids else {}
        self.links = _links(db, min(tids, default=0))
        self.pushed = _pushed(db)
        self.landings = db.q("SELECT task, sha FROM task_landings ORDER BY marker")
        self.superseded = db.q("SELECT task, head, sha, ts FROM task_superseded ORDER BY ts")
        self.dropped = db.q("SELECT task, sha, ts FROM task_dropped")


def carriers(db, tid: int, rows: Rows | None = None) -> dict[int, tuple[str, int | None, str]]:
    """{task: (branch, the carrier it follows, kind)} of task `tid` and every task that carries its change on: a review of it
    (review_subject), a task that `continues:` or fixes (`review_fix:`) one of them, rebases or carries
    one of their branches (`rebases:`/`pr_branch:`) or whose title rebases one ("rebased #<id>"), and
    so on down the chain (a re-review of a fix of a review of it). A follow-up (`follows:`) counts
    only when it works on its predecessor's own branch, so it carries that change: a plain follow-up
    of a done task starts from the base branch and never makes an unlanded change count. When the push queue lands such a
    successor, `tid`'s work landed, under other patches if a conflict was settled on the way. `rows`:
    read once for many tasks (Rows), else read here."""
    if rows is None or tid not in rows.tasks:
        links, task = _links(db, tid), db.task(tid)
    else:
        links, task = [lk for lk in rows.links if lk[0] > tid], rows.tasks[tid]
    out = {tid: ((task or {}).get("branch") or "", None, (task or {}).get("kind") or "")}
    grew = True
    while grew:   # a successor always has a later id, but a link may name a branch rather than an id
        grew = False
        branches = {v[0]: k for k, v in out.items() if v[0]}
        for i, b, kind, ids, names, follows in links:
            on = max(ids & out.keys(), default=None) or branches.get(min(names & branches.keys(), default="")) \
                or max((f for f in follows & out.keys() if b and b == out[f][0]), default=None)
            if i not in out and on is not None:
                out[i], grew = (b, on, kind), True
    return out


def _pushed(db) -> list[dict]:
    try:
        return db.q("SELECT q.task, q.branch, q.landed_sha, q.pushed_sha FROM push_queue q "
                    "WHERE q.status IN ('pushed','landed') ORDER BY q.id")
    except Exception:
        return []


def _gone_shas(db, tid: int, carry: dict, rows: Rows | None) -> set[str]:
    """The shas whose drop verdict still stands for task `tid`: a drop beats every landing record at
    that sha until a supersession is recorded after it, and a drop recorded after the supersession
    wins again (the record times decide)."""
    dropped = db.q("SELECT task, sha, ts FROM task_dropped WHERE task=?", (tid,)) if rows is None \
        else [r for r in rows.dropped if r["task"] == tid]
    latest: dict = {}
    found = db.q("SELECT sha, ts FROM task_superseded WHERE task IN (%s)" % ",".join("?" * len(carry)),
                 list(carry)) if rows is None else [r for r in rows.superseded if r["task"] in carry]
    for r in found:
        latest[r["sha"]] = max(latest.get(r["sha"], 0.0), float(r["ts"]))
    return {r["sha"] for r in dropped if float(r["ts"]) >= latest.get(r["sha"], 0.0)}


def recorded(db, tid: int, carry: dict | None = None, rows: Rows | None = None) -> tuple[list[str], str]:
    """The landed commits saved for task `tid` or a task that carries it on (`carry`, else
    carriers): a push queue row it or its branch landed in, a successful detached push, a recorded
    supersession; ([], "") when none. `ttp landed --task` and deadwait both judge from this; `rows`:
    read once for many tasks (Rows), else read here."""
    carry = carriers(db, tid, rows) if carry is None else carry
    branches = {v[0] for v in carry.values() if v[0]}

    def via(src: str, i) -> str:
        """`src`, naming the successor it came through (not `tid` itself or a plain review of it)."""
        direct = i == tid or i not in carry or (carry[i][1] == tid and carry[i][2] == "review")
        return src if direct else f"{src} through #{i}"
    # A batch member shares its lead's branch, but a landing of that branch is not its own: it landed
    # only once its own commit is on the push branch (batchrun.landings records it as `landed`).
    task = (rows.tasks.get(tid) if rows is not None else None) or db.task(tid) or {}
    res = load_result(task.get("result"))
    member = isinstance(res, dict) and isinstance(res.get("batch"), dict) and res["batch"].get("lead") != tid
    if member and res.get("landed"):
        return [str(res["landed"])], "its own commit on the push branch"
    gone = _gone_shas(db, tid, carry, rows)
    pushed = _pushed(db) if rows is None else rows.pushed
    hits = [r for r in pushed if (r["landed_sha"] or r["pushed_sha"]) not in gone | {None, ""}
            and (r["task"] in carry or (r["branch"] in branches and not member))]
    if hits:
        return [r["landed_sha"] or r["pushed_sha"] for r in hits], via("its push queue landing", hits[-1]["task"])
    landings = db.q("SELECT task, sha FROM task_landings ORDER BY marker") if rows is None else rows.landings
    hits = [r for r in landings if r["task"] in carry and r["sha"] not in gone]
    if hits:
        return list(dict.fromkeys(r["sha"] for r in hits)), via("its detached push landing", hits[-1]["task"])
    if rows is None:
        found = db.q("SELECT task, head, sha FROM task_superseded WHERE task IN (%s) ORDER BY ts"
                     % ",".join("?" * len(carry)), list(carry))
    else:
        found = [r for r in rows.superseded if r["task"] in carry]
    found = [r for r in found if r["sha"] not in gone]
    if found:
        return [r["sha"] for r in found], via("the commit recorded as superseding it", found[-1]["task"])
    return [], ""


def landed_at(db, tid: int, rows: Rows | None = None, carry: dict | None = None) -> float | None:
    """When task `tid`'s change first landed, from the push records `recorded` judges by (a push queue
    landing of it or a task carrying it on, a detached push, a recorded supersession); None while none
    exists. A task of a kind that is not pushed (harness, work, question) lands when it is done.
    `rows`: read once for many tasks (Rows), else read here; `carry`: carriers read by the caller."""
    task = rows.tasks.get(int(tid)) if rows else db.task(int(tid))
    if not task:
        return None
    carry = carriers(db, int(tid), rows) if carry is None else carry
    res = load_result(task.get("result"))
    member = isinstance(res, dict) and isinstance(res.get("batch"), dict) and res["batch"].get("lead") != tid
    branches = {v[0] for v in carry.values() if v[0]} if not member else set()
    marks = ",".join("?" * len(carry))
    gone = _gone_shas(db, int(tid), carry, rows)   # the drop rule recorded() judges by
    times = []
    try:
        times += [r["updated"] for r in db.q(
            f"SELECT updated, COALESCE(landed_sha, pushed_sha) AS sha FROM push_queue WHERE "
            f"status IN ('pushed','landed') AND (landed_sha IS NOT NULL OR pushed_sha IS NOT NULL) "
            f"AND (task IN ({marks}) OR branch IN (SELECT value FROM json_each(?)))",
            (*carry, json.dumps(sorted(branches)))) if r["sha"] not in gone]
    except Exception:
        pass   # a database without the push queue
    for table in ("task_landings", "task_superseded"):
        times += [r["ts"] for r in db.q(f"SELECT ts, sha FROM {table} WHERE task IN ({marks})", list(carry))
                  if r["sha"] not in gone]
    if not times and task.get("status") == "done" and (task.get("kind") != "code" or (member and res.get("landed"))):
        times.append(task.get("updated"))
    times = [float(t) for t in times if t]
    return min(times) if times else None


def landed_whens(p, tids, rows: Rows | None = None) -> dict[int, float | None]:
    """{task: when its change first reached the push branch} for each task in `tids`: the record time
    `landed_at` judges by, or, when a recorded supersession or squash carried it (one whose drop
    verdict does not stand), the earlier committer date of the superseding commit or of the head it
    names once that head was on the push branch (on_push). A head's date is when it was committed on
    its branch, which a fast-forward carried unchanged. A push queue landing keeps its record time: its
    commit is dated at the rebase, before its checks ran. A commit older than the task (work that
    existed already) lands it when it was created. None while nothing is known. `rows`: read once for all
    of them (Rows), else read here."""
    tids = [int(t) for t in tids]
    rows = Rows(p.db, tids) if rows is None else rows
    at, want, tip = {}, {}, None
    for tid in tids:
        carry = carriers(p.db, tid, rows)
        at[tid] = landed_at(p.db, tid, rows, carry)
        gone = _gone_shas(p.db, tid, carry, rows)
        sup = [r for r in rows.superseded if r["task"] in carry and r["sha"] not in gone]
        heads = [str(r["head"]) for r in sup if r.get("head")]
        if heads and tip is None:
            try:
                tip = _commit(p.root, _push_ref(p, p.root))
            except (ValueError, OSError, subprocess.SubprocessError):
                tip = ""
        want[tid] = [str(r["sha"]) for r in sup] + [h for h in heads if tip and on_push(p, p.root, [h], tip)]
    dates = commit_dates(p, [s for v in want.values() for s in v])
    made = {tid: float((rows.tasks.get(tid) or {}).get("created") or 0) for tid in tids}
    return {tid: min([t for t in [at[tid], *(max(dates[s], made[tid]) for s in want[tid] if s in dates)] if t],
                     default=None) for tid in tids}


def commit_dates(p, shas) -> dict[str, float]:
    """{sha: committer date} of the shas the repository has, in one git call (unknown ones are left
    out)."""
    shas = [s for s in dict.fromkeys(str(s) for s in shas) if s]
    out: dict[str, float] = {}
    if shas:
        r = _git(p.root, "log", "--no-walk", "--ignore-missing", "--format=%H %ct", *shas)
        for ln in r.stdout.splitlines() if r.returncode == 0 else []:
            sha, _, ts = ln.partition(" ")
            if ts.isdigit():
                out[sha] = float(ts)
    return out


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
    if lost := dropped(db, tid):   # its branch is still an ancestor after a revert: never read as landed
        return [], f"#{tid} dropped: {lost}"
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


def commits_sha(p, shas: list[str], tip: str = "") -> str:
    """The push branch commit (local ref, no fetch; `tip` when given) that carries every commit of `shas`
    (on_branch: an ancestor, or an equivalent there such as a rebased copy) when none of them is reverted
    there ("This reverts commit <sha>"); "" when one is missing, none is given or git cannot read them. A
    batch run's hand-off lists each member's own commits, so this finds a member's landing with no trailer
    in its message."""
    if not shas or not tip and not has_push_branch(p):
        return ""
    try:
        tip = _commit(p.root, tip or _push_ref(p, p.root))
        full = [_commit(p.root, s) for s in shas]
    except ValueError:
        return ""
    if not tip or not all(full):
        return ""
    reverts = _git(p.root, "log", "--format=%B", "--grep=This reverts commit", tip).stdout
    gone = set(re.findall(r"This reverts commit ([0-9a-f]{40,64})", reverts))
    return tip if not gone & set(full) and all(on_branch(p.root, s, tip) for s in full) else ""


def trailer_sha(p, tid: int, since: float | None = None, tip: str = "") -> str:
    """The newest commit on the push branch (local ref, no fetch) whose message carries task `tid`'s
    own `Task: #<id>` trailer and that no later commit there reverts ("This reverts commit <sha>"),
    committed after `since`; "" when none, or no push branch. Batch runs before commits_sha committed each
    member with that trailer, so this finds such a member's landing that no hand-off names. `tip`: look
    from that commit (one the push queue landed) instead of the push branch's ref."""
    if not tip and not has_push_branch(p):
        return ""
    try:
        tip = _commit(p.root, tip or _push_ref(p, p.root))
    except ValueError:
        return ""
    if not tip:
        return ""
    after = [f"--since=@{int(since)}"] if since else []
    shas = _git(p.root, "log", "--format=%H", "-E", f"--grep=^Task: #{int(tid)}([^0-9]|$)", *after, tip).stdout.split()
    if not shas:
        return ""
    reverts = _git(p.root, "log", "--format=%B", "--grep=This reverts commit", *after, tip).stdout
    gone = set(re.findall(r"This reverts commit ([0-9a-f]{40,64})", reverts))
    return next((s for s in shas if s not in gone), "")


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

    A code task in 'review', or one of another kind (not a review) with a branch on record, is accepted
    on the same checks (its work landed), a harness task in 'review' or done (with no `ref` or `repo`)
    when `sha` is merged into the harness checkout's HEAD and its worktree branch has nothing unmerged
    (_record_harness); one left in 'review' is then closed as done. When `ref` names the branch of
    an open task (_heir), that task carries the work: this one's chat hears nothing from it and _hand_over gives
    the reply to that task; any other record posts the landing reply."""
    task = p.db.task(tid)
    if not task:
        raise ValueError(f"no task #{tid}")
    status, kind = task.get("status"), task.get("kind")
    if status != "done" and not (status == "review" and (kind in ("code", "harness") or (
            kind != "review" and str(task.get("branch") or "").strip()))):
        raise ValueError(f"not recorded: task #{tid} is {status}, not done"
                         + (" or a code or harness task, or one with a branch, in review" if status == "review" else ""))
    if kind == "harness" and not ref and not repo:
        words = _record_harness(p, task, sha)
    else:
        words = _record_work(p, task, sha, ref, repo)
    heir = _heir(p, tid, ref) if ref else None
    landed = not heir   # else an open task carries the work and the reply: this one posts nothing
    if heir and (status == "review" or replymsg.HELD in load_result(task.get("result"))):
        words += _hand_over(p, task, heir)
    if status == "review":
        full = p.db.one("SELECT sha FROM task_superseded WHERE task=? ORDER BY ts DESC LIMIT 1", (tid,))["sha"]
        words += _close_review(p, task, full, landed)
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


def _heir(p, tid: int, ref: str | None) -> dict | None:
    """The open, non-review task whose branch `ref` names (a local branch, or one behind a remote's name:
    only a remote prefix is stripped), when the record names one: the task that carries superseded `tid`'s work."""
    from . import obligations
    name = str(ref or "").removeprefix("refs/heads/")
    if name.startswith("refs/remotes/"):
        name = name.removeprefix("refs/remotes/").split("/", 1)[-1]
    elif "/" in name and name.split("/", 1)[0] in _git(p.root, "remote").stdout.split():
        name = name.split("/", 1)[1]
    return next((t for t in p.db.q(
        "SELECT id, branch, reply_chat FROM tasks WHERE id<>? AND kind<>'review' AND branch<>'' "
        f"AND status NOT IN ({','.join('?' * len(obligations.CLOSED))}) ORDER BY id DESC",
        (tid, *obligations.CLOSED)) if name and t["branch"] == name), None)


def _hand_over(p, task: dict, heir: dict) -> str:
    """Superseded `task` was re-landed by open task `heir`, not yet on the push branch, so its chat hears
    nothing from it: its held reply (replymsg.HELD) is dropped, and `heir` takes its reply chat, when it has
    none, and the obligations and promises it owns. The words to add."""
    from . import obligations
    from .db import dump_result
    tid = task["id"]
    with p.db.tx():
        row = p.db.task(tid) or task
        prev = load_result(row.get("result"))
        if prev.pop(replymsg.HELD, None) is not None:
            p.db.update_task(tid, result=dump_result(prev))
        if row.get("reply_chat") and not heir["reply_chat"]:
            p.db.update_task(heir["id"], reply_chat=row["reply_chat"])
        moved = obligations.hand_over(p, tid, heir["id"], f"#{heir['id']} carries superseded #{tid}'s work on")
    return f"; #{heir['id']} carries its reply" + (f" and {', '.join(moved)}" if moved else "")


def _close_review(p, task: dict, sha: str, landed: bool = True) -> str:
    """Close `task`, left in 'review', as done now that `sha` carries its work, with the chat reply
    (pushq._reply) when this closed it and `sha` `landed`: the words to add."""
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
        if landed:
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
    # The task's own head, already on the --ref target (its own push was cancelled and the work landed
    # another way), is the work itself; any older commit of the branch still predates it. Without --ref
    # the holders above may be only the branch's own tracking ref, which proves no landing.
    own_head = bool(ref) and full == head
    if not own_head and _git(repo, "merge-base", "--is-ancestor", full, head).returncode == 0:
        raise ValueError(f"not recorded: {full[:10]} is already in branch {branch}, so it predates the work")
    base = _git(repo, "merge-base", head, tip).stdout.strip() if tip else ""
    if not base:
        raise ValueError(f"not recorded: branch {branch} shares no history with {ref or 'the push branch'}")
    theirs = set(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "--root", "-m",
                      "--first-parent", "-z", full).stdout.split("\0")) - {""}
    mine = set(_git(repo, "diff", "--name-only", "--no-renames", "-z", base, head).stdout.split("\0")) - {""}
    on_ref = bool(ref) and any(_commit(repo, r) and _git(repo, "merge-base", "--is-ancestor", full, r).returncode == 0
                               for r in (f"refs/heads/{ref}", f"refs/remotes/origin/{ref}", ref))
    if not on_ref and not own_head and not theirs & mine:
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
    from . import push   # late: push loads pushq, whose imports reach this module
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


DROP_EVENT = "landing_dropped"


def dropped(db, tid: int) -> str:
    """Why task `tid`'s recorded landings do not carry its change (drop), the latest first; "" when none was."""
    r = db.one("SELECT why FROM task_dropped WHERE task=? ORDER BY ts DESC LIMIT 1", (int(tid),))
    return r["why"] if r else ""


def _revived_shas(db, tid: int) -> set[str]:
    """Shas of task `tid` whose drop verdict a supersession recorded after it beat (the record times
    _gone_shas judges by): they count as landed until a drop newer than that supersession."""
    gone = _gone_shas(db, tid, carriers(db, tid), None)
    return {r["sha"] for r in db.q("SELECT sha FROM task_dropped WHERE task=?", (tid,))} - gone


def drop(p, tid: int, shas: list[str], why: str, event: bool = True, recheck: bool = False) -> list[str]:
    """Record that the landings `shas` recorded for task `tid` do not carry its change (a successor's rebase or
    batch left its commits out, a forward revert took them off): recorded,
    landed_at and the tried ledger stop counting them, and `landed:#<id>` waits, naming `why`. A later landing
    or `ttp task <project> superseded <id> <sha>` counts again. `event`: wake the coordinator once per task.
    Returns the shas it recorded; `recheck` (an automatic re-derivation of the same verdict) records
    none of the shas a supersession won back, so the recorded verdict, not the re-derived one, stands."""
    now = time.time()
    revived = _revived_shas(p.db, tid) if recheck else set()
    live = [s for s in dict.fromkeys(s for s in shas if s) if s not in revived]
    for sha in live:
        p.db.x("INSERT INTO task_dropped(task, sha, why, ts) VALUES(?,?,?,?) "
               "ON CONFLICT(task, sha) DO UPDATE SET why=excluded.why, ts=excluded.ts",
               (int(tid), sha, why, now))
    fp = f"{DROP_EVENT}:{int(tid)}:{min(live, default='')[:12]}"   # a later drop of a re-landed change wakes again
    if event and live and not p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
               (now, "daemon", DROP_EVENT, fp, "normal",
                f"#{tid} was recorded as landed, but its change is not on the push branch: {why}. Re-land it "
                f"(a code task that continues #{tid}), or record where it did land with `ttp task <project> "
                f"superseded {tid} <sha>`; landed:#{tid} waits until then."[:4000], "queued", int(tid)))
    return live



def carried(p, repo: Path, tid: int, tip: str, shas: list[str] | None = None, src: str = "",
            rows: Rows | None = None) -> str:
    """Why task `tid`'s recorded landing (`shas`/`src`, else recorded) does not carry its change: the head
    it handed off (integrate.member_head) is in none of those landed commits (on_branch: an ancestor, or an
    equivalent by patch, author and subject, or replay trailer; each a short range from where it forked),
    not on the push branch's `tip` or a retired tip (on_push), and none holds most of its lines (_holds_part).
    "" when it is, when nothing is recorded, or when no head was recorded to judge by."""
    from . import integrate   # late: integrate loads worktree, whose imports reach this module
    if shas is None:
        shas, src = recorded(p.db, tid, rows=rows)
    head = integrate.member_head(repo, tid) if shas else ""
    if not head or any(_commit(repo, s) and on_branch(repo, head, _commit(repo, s)) for s in shas) \
            or on_push(p, repo, [head], tip) \
            or any(_commit(repo, s) and _holds_part(repo, head, _commit(repo, s)) for s in shas):
        return ""
    return (f"{src or 'its landing'} ({', '.join(s[:10] for s in shas)}) does not hold its own head "
            f"{head[:10]}, which is on no push branch tip")


# Release bookkeeping a later batch rewrites anyway: never evidence that a change landed or was lost
_BOOKKEEPING = (":!*.changeset/*", ":!*plugin.json", ":!*/__init__.py", ":!*smoke.txt")


def _holds_part(repo: Path, head: str, sha: str) -> bool:
    """Whether commit `sha` holds most of `head`'s change from where they forked: at least half its added
    lines (bookkeeping files and short lines aside) are lines of the same file at `sha`. A review fix,
    rebase or narrowing that reworked the commits keeps most of them; a successor that dropped them keeps
    few (only lines the files already had). True when there is nothing to judge."""
    mb = _git(repo, "merge-base", head, sha).stdout.strip()
    added = held = 0
    for f in _git(repo, "diff", "--name-only", mb, head, "--", ".", *_BOOKKEEPING).stdout.split() if mb else []:
        have = {x.strip() for x in _git(repo, "show", f"{sha}:{f}").stdout.splitlines()}
        for x in _git(repo, "diff", "-U0", mb, head, "--", f).stdout.splitlines():
            if x.startswith("+") and not x.startswith("+++") and len(x.strip()) > 12:
                added, held = added + 1, held + (x[1:].strip() in have)
    return 2 * held >= added


def drops(p, days: float = 14, record: bool = True, skip: set[int] | None = None) -> list[tuple[int, str]]:
    """[(task, why)] of each done code task of the last `days` recorded as landed whose change is not on the
    push branch (carried). `record`: drop each one (an event apiece); `skip`: tasks already found carried,
    and each found carried is added to it. The daemon runs this once per push-branch tip."""
    repo = p.root
    try:
        tip = _commit(repo, _push_ref(p, repo))
    except ValueError:
        return []
    if not tip:
        return []
    skip = set() if skip is None else skip
    ids = [t["id"] for t in p.db.q(
        "SELECT id FROM tasks WHERE kind='code' AND status='done' AND COALESCE(updated, 0) >= ? AND "
        "id NOT IN (SELECT task FROM task_abandoned) ORDER BY id", (time.time() - days * 86400,)) if t["id"] not in skip]
    rows, out = Rows(p.db, ids), []
    for tid in ids:
        shas, src = recorded(p.db, tid, rows=rows)
        if not shas:
            continue
        if why := carried(p, repo, tid, tip, shas, src):
            if record and not drop(p, tid, shas, why, recheck=True):
                skip.add(tid)   # a supersession recorded after its drop keeps the sha landed
            else:
                out.append((tid, why))
        else:
            skip.add(tid)
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
    entry of one of them or its branch still waiting to push. The daemon's own open review of it
    (auto_review) is cancelled in the same step instead, so it is no reason to refuse."""
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
    own = []   # the daemon's own review of it: cancelled with the abandon, not left to block it
    for t in p.db.q(f"SELECT * FROM tasks WHERE id!=? AND status NOT IN ({','.join('?' * len(TERMINAL_TASK_STATES))}) "
                    f"AND (id IN ({marks}) OR kind='review') ORDER BY id", (tid, *TERMINAL_TASK_STATES, *ids)):
        # an open review that covers it as the daemon's auto-review reads it: by dependency or a named branch
        if not (t["id"] in carry or tid in dependency_ids(t) or (branch and branch in named_refs(p, t))):
            continue
        if t["kind"] == "review" and t.get("origin") == "daemon" and f'"auto_review:{tid}"' in (t["labels"] or ""):
            own.append(t["id"])
            continue
        raise ValueError(f"not abandoned: task #{tid}'s work can still land: {t['kind']} task #{t['id']} "
                         f"that carries it is {t['status']}: cancel that first")
    with p.db.tx():
        p.db.x("INSERT INTO task_abandoned(task, why, ts) VALUES(?,?,?)", (tid, why, time.time()))
        for rid in own:
            p.db.cancel(rid)
    return (f"task #{tid}: abandoned ({why}); its branch {task.get('branch') or '(none)'} is kept"
            + (f"; its review {', '.join(f'#{i}' for i in own)} cancelled" if own else ""))


def landed_probe(p) -> str:
    """`delivery.landed_probe`: a read-only shell command template, `{id}` the task id (exit 0 landed,
    1 not yet, any other exit broken), that answers `landed:#<id>` in a project with no
    `delivery.push_branch`; "" when unset. Ignored while a push branch is set."""
    return str((p.config().get("delivery") or {}).get("landed_probe") or "").strip()


def has_push_branch(p) -> bool:
    """Whether delivery.push_branch is set: a landing can be checked against the push branch."""
    return bool(str((p.config().get("delivery") or {}).get("push_branch") or "").strip())


_has_push_branch = has_push_branch   # daemon.py still calls the private name


def can_check(p) -> bool:
    """Whether a `landed:#<id>` wait can ever pass here: a push branch, else a landed probe. Without
    either the harness chains with depends_on, the coordinator's task_add/task_update turn `landed:#<id>`
    into depends_on, `ttp task` refuses it (NO_CHECK), and the daemon's probes (DONE_ENV) wait on the task being done."""
    return has_push_branch(p) or bool(landed_probe(p))


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


def _not_dropped(p, repo: Path, tid: int | None, tip: str, shas: list[str], src: str, onto: str) -> tuple[int, str] | None:
    """check's NOT_YET for task `tid` whose landing record is on the push branch but does not carry its own
    change (carried; it is dropped there and then), else None. Its own branch's commits need no such check."""
    if tid is None or p is None or src.startswith("its branch") or onto != _push_ref(p, repo):
        return None
    if not (lost := carried(p, repo, tid, tip, shas, src)):
        return None
    if not drop(p, tid, shas, lost, recheck=True):
        return None   # a supersession recorded after its drop keeps the sha landed
    return NOT_YET, f"#{tid} dropped: {lost}"


def check(p, repo: Path, sha: str | None = None, tid: int | None = None, onto: str | None = None,
          fetch: bool = True, resolved: tuple[list[str], str] | None = None) -> tuple[int, str]:
    """(exit code, words) for `ttp landed`: a sha, or task `tid`'s landing, on `onto` (default the
    push branch). Reads the local ref first and fetches only when that says not yet (never with
    `fetch` False: local refs only). With no push branch, a task's landing is delivery.landed_probe's
    answer (with `fetch` False it is not run: NOT_YET), and without that key CANNOT naming the fix.
    `resolved`: the (shas, src) task_shas returns, when the caller already read it."""
    if tid is not None and onto is None and not has_push_branch(p):
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
        elif resolved is not None:
            shas, src = resolved
        else:
            shas, src = task_shas(p, tid)
        if not shas:
            return NOT_YET, src
        what = f"#{tid} ({src})"
    else:
        full = _commit(repo, sha or "")
        if not full:
            return CANNOT, f"no commit {sha} in {repo}"
        shas, what, src = [full], (sha or "")[:10], ""
    for fetching in (False, True) if fetch else (False,):
        tip, ref = _tip(repo, onto, fetching)
        if not tip:
            if fetching:
                return CANNOT, ref
            continue
        if any(_commit(repo, s) and on_branch(repo, s, tip) for s in shas):
            return _not_dropped(p, repo, tid, tip, shas, src, onto) or (0, f"{what} is on {ref}")
        gone = [reverted_by(repo, s, tip) for s in shas if _commit(repo, s)]
        if r := next((g for g in gone if g), ""):
            return NOT_YET, f"{what} reverted by {r[:10]} on {ref}"
        if None in gone:   # the revert test could not run: unverified, never "not landed"
            return CANNOT, f"cannot tell whether {what} was reverted on {ref}"
    if p is not None and has_push_branch(p) and onto == _push_ref(p, repo) and (found := on_push(p, repo, shas, "")):
        return _not_dropped(p, repo, tid, tip, shas, src, onto) or (0, f"{what} is on {onto}, at its retired tip {found[:10]}")
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
