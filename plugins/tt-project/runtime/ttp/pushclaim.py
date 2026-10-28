# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Pushes a hand-off claims, checked on the remote before its task is recorded done.

A done hand-off that claims a push (a "pushed <sha> to <ref>" line in its summary, a `pushed` list in
result.json, or a detached `ttp push` its run started) leaves its task in `pushing` (daemon
_finish_worker). The daemon then asks the remote, model-free (verify_pushes): once every claimed
commit is the tip of its ref per `git ls-remote`, or reachable from it after a fetch, the hand-off is
recorded as usual. A ref or commit the remote definitely lacks fails the task with the remote, ref and
sha; a remote that cannot be read keeps it in `pushing` and asks again with backoff, up to BOUND_S,
after which it fails too. Push-queue landings are checked against the commit the queue recorded
(landed_sha), since its rebase gives commits new shas.

`ttp push-audit` lists done tasks of the last days whose claims are not on the remote, with the same
check; the daemon runs it every AUDIT_EVERY_S, raises one keyed observation per gap and puts one line
in the daily review. It also flags a done recorded from a checks-wait `on_pass` while the hand-off
named an on_pass_cmd that never ran (no on_pass/ folder in its run).

In the daemon both skip while the core provider's network is held, and share a Budget of
NET_BUDGET_S of git network time per tick: what it leaves unchecked waits, untouched, for a later
tick (OutOfTime), which reuses the answers read so far for up to CARRY_S. A carried answer never
alone fails a claim: before a MISSING that rests on one, the remote is asked again.

A detached push's claim the remote cannot settle either way is UNVERIFIABLE, counted apart from MISSING:
one whose target cannot be determined, or whose push ended without writing a failed outcome (exit
null: a reboot, a killed run, a push queued and never started) while its commit is not on the target.
Its repo is the one the marker names, else (a task worktree since pruned) the project root.
Accepted gap: when that fallback root is another repository than the push's, a remote it lacks makes
the claim UNVERIFIABLE rather than checked on the remote the push used. The audit confirms such a
claim, or one left UNKNOWN, when its commit is an ancestor of the target's local tracking ref."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

SHARED = "shared"                # a claim on another task's commit
KEY = "push_verify"              # tasks.result of a task in `pushing` until its claims are verified
HANDOFF = "push_claim_handoff.json"   # the hand-off waiting on the check, in its run's folder
AUDIT_KV = "push_audit"          # the last audit: {"at", "days", "counts", "gaps"}
CONFIRMED, MISSING, UNKNOWN = "confirmed", "missing", "unknown"
UNVERIFIABLE = "unverifiable"     # a claim neither confirmed nor shown missing (module doc)
BACKOFF_S = (60, 120, 300, 600, 1200, 1800)
BOUND_S = 6 * 3600               # a claim the remote never answered for this long fails its task
LS_TIMEOUT_S, FETCH_TIMEOUT_S = 20, 40
PER_TICK = 4                     # tasks checked per daemon tick
NET_BUDGET_S = 30                # git network time (ls-remote, fetch) push checks spend per daemon tick
CARRY_S = 120                    # how long an out-of-time tick's remote answers are reused by later ticks
CARRIED = "carried"              # a cache's keys read on an earlier tick (a set)
AUDIT_EVERY_S, AUDIT_DAYS, AUDIT_REFS = 3600, 2, 20
ON_PASS_DIR = "on_pass"          # daemon.ON_PASS_DIR

# A short sha may have no digit (about 1 in 1,000 at 7 characters): a token counts only once it resolves
# as a commit it prefixes (text_claims), which a word or a ref name does not.
_SHA = r"`?([0-9a-f]{7,40})`?"
_REF = r"`?([A-Za-z0-9][\w./@+-]*\w)`?"
# "pushed abc1234 to dev", "landed as abc1234 on origin/dev", "pushed to dev as abc1234", "pushed tag v1 at abc1234"
_TAG = re.compile(rf"\bpushed\s+(?:the\s+)?tag\s+{_REF}\s+(?:at|to|on)\s+{_SHA}", re.I)
_REF_SHA = re.compile(rf"\b(?:pushed|landed)\s+(?:to|on|onto)\s+{_REF}\s+(?:as|at)\s+{_SHA}", re.I)
_SHA_REF = re.compile(rf"\b(?:pushed|landed)\s+(?:commit\s+)?(?:as\s+|at\s+)?{_SHA}(?:\s+(?:to|on|onto)\s+{_REF})?",
                      re.I)
_NEGATED = re.compile(r"(?:\bnot|\bnever|n't|\bbe|\bwill|\bwould|\bto)"
                      r"(?:\s+(?:yet|been|being|get|got|gotten|already|actually|really|ever))*\s+$", re.I)
_NOT_REFS = {"the", "a", "an", "it", "its", "origin", "remote", "github", "branch", "upstream"}


def _git(repo, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 124, "", f"{type(e).__name__}: {e}")


def _commit(repo, rev: str) -> str:
    r = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else ""


def _remotes(repo) -> list[str]:
    return _git(repo, "remote").stdout.split()


def _ref(repo, text: str, tag: bool = False) -> tuple[str, str] | None:
    """(remote, full ref) a claim names: `<remote>/<branch>`, a branch of origin, refs/heads/...,
    refs/tags/... or a tag. None when it names no remote of `repo`."""
    from .push import _split_target
    text = text.strip().strip("`").removeprefix("refs/remotes/")
    if text.startswith("refs/tags/"):
        text, tag = text[len("refs/tags/"):], True
    remotes = _remotes(repo)
    remote, name = _split_target(text, remotes)
    if remote not in remotes or not name:
        return None
    return remote, f"refs/tags/{name}" if tag else f"refs/heads/{name}"


def short_ref(remote: str, ref: str) -> str:
    return (f"{remote}/{ref[len('refs/heads/'):]}" if ref.startswith("refs/heads/")
            else f"tag {ref[len('refs/tags/'):]} on {remote}" if ref.startswith("refs/tags/") else f"{remote} {ref}")


def _default(p, repo) -> tuple[str, str] | None:
    """The project's push branch, where a claim that names no ref went."""
    from . import push
    try:
        remote, branch = push.target(p, Path(repo))
    except (ValueError, OSError):
        return None
    return (remote, f"refs/heads/{branch}") if remote in _remotes(repo) else None


def text_claims(p, text: str, repo=None) -> list[dict]:
    """The pushes a summary claims. Only commits of the project's repository count (a claim about
    another repository's commit cannot be checked here), and only remotes it has."""
    repo = repo or p.root
    out, seen = [], set()
    found = []
    for rx, order in ((_TAG, "ref_sha"), (_REF_SHA, "ref_sha"), (_SHA_REF, "sha_ref")):
        for m in rx.finditer(text or ""):
            if _NEGATED.search(text[max(0, m.start() - 40):m.start()]):
                continue
            ref, sha = (m.group(1), m.group(2)) if order == "ref_sha" else (m.group(2), m.group(1))
            if any(m.start() < e and s < m.end() for s, e in found):
                continue   # one phrase, one claim
            found.append((m.start(), m.end()))
            full = _commit(repo, sha)
            if not full or not full.startswith(sha.lower()):
                continue
            target = None
            if ref and ref.lower() not in _NOT_REFS:
                target = _ref(repo, ref, tag=rx is _TAG)
            target = target or _default(p, repo)
            if not target or (full, target) in seen:
                continue
            seen.add((full, target))
            out.append({"repo": str(repo), "remote": target[0], "ref": target[1], "sha": full,
                        "source": "its summary", "strict": False})
    return out


def _structured(p, result: dict) -> list[dict]:
    """A hand-off's own `pushed` list: [{"sha", "ref" | "branch" | "tag", "remote"?}]. Entries the push
    queue wrote (with a batch) are checked from its rows instead (queue_claims)."""
    raw = result.get("pushed")
    raw = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    out = []
    for e in raw:
        if not isinstance(e, dict) or e.get("batch") is not None or not isinstance(e.get("sha"), str):
            continue
        full = _commit(p.root, e["sha"].strip())
        name = e.get("tag") or e.get("ref") or e.get("branch")
        if not full:
            continue
        if isinstance(name, str) and name.strip():
            name = name.strip()
            if isinstance(e.get("remote"), str) and e["remote"].strip() and not name.startswith("refs/"):
                name = f"{e['remote'].strip()}/{name}"
            target = _ref(p.root, name, tag=bool(e.get("tag")))
        else:
            target = _default(p, p.root)
        if target:
            out.append({"repo": str(p.root), "remote": target[0], "ref": target[1], "sha": full,
                        "source": "its pushed list", "strict": True})
    return out


def _markers(p, tid: int) -> list[tuple[Path, dict]]:
    from . import push
    out = []
    for marker in sorted((p.state / push.DETACHED).glob(f"t{tid}-*.json")):
        m = push._read(marker)
        if isinstance(m, dict) and m.get("kind") != "batch":
            out.append((marker, m))
    return out


def marker_claims(p, tid: int, run_id) -> list[dict]:
    """The detached pushes a run started (and any of the task's still queued or running): each is a
    claim, checked once it wrote its outcome (_check_marker)."""
    out = []
    for marker, m in _markers(p, tid):
        mine = run_id is not None and str(m.get("run")) == str(run_id)
        if mine or m.get("status") in ("queued", "running"):
            out.append({"marker": marker.name, "source": f"its detached push {marker.stem}", "strict": True})
    pushed = {c["marker"] for c in out if _read_marker(p, c["marker"]).get("status") == "pushed"}
    # A push that failed and was then run again: the one that pushed is the claim.
    return [c for c in out if not pushed or c["marker"] in pushed
            or _read_marker(p, c["marker"]).get("status") in ("queued", "running")]


def queue_claims(p, tid: int) -> list[dict]:
    """The push queue's landings of task `tid`: the commit it recorded on its target."""
    out = []
    try:
        rows = p.db.q("SELECT branch, landed_sha, pushed_sha, target FROM push_queue WHERE task=? "
                      "AND status IN ('pushed','landed') ORDER BY id", (tid,))
    except Exception:
        return out
    for r in rows:
        sha = r["landed_sha"] or r["pushed_sha"]
        target = _ref(p.root, r["target"]) if r["target"] else None
        if sha and target:
            out.append({"repo": str(p.root), "remote": target[0], "ref": target[1], "sha": sha,
                        "source": "its push queue landing", "strict": True})
    return out


def _self_stopped(p, c: dict) -> bool:
    """A detached push that ended on a stop signal (push._stop wrote its reason) before pushing."""
    m = _read_marker(p, c["marker"])
    return m.get("status") == "failed" and str(m.get("reason") or "").startswith("stopped by SIG")


def _splits(result: dict) -> bool:
    """A hand-off that leaves its work to a `continue:` follow-up (chaincap.CONT_TITLE)."""
    from .chaincap import CONT_TITLE
    fups = result.get("followups")
    return isinstance(fups, list) and any(isinstance(f, dict) and CONT_TITLE.match(str(f.get("title") or ""))
                                          for f in fups)


def claims(p, task: dict, result: dict, run: dict | None) -> list[dict]:
    """What a done hand-off claims was pushed: its summary's lines, its `pushed` list and its run's
    detached pushes. Local reads only (no network): it runs inside the run's finishing transaction.
    A split hand-off (a `continue:` follow-up) that claims no push itself does not claim a detached
    push its run stopped: the continuing task delivers the work. Accepted gap: a push cannot learn who
    sent its stop signal (Python handlers get no siginfo), so such a hand-off is trusted to have sent
    it; a hand-off that claims a push, or a push that ended any other way, stays a claim."""
    if not isinstance(result, dict):
        return []
    found = marker_claims(p, task["id"], (run or {}).get("id"))
    own = _structured(p, result) + text_claims(p, str(result.get("summary") or ""))
    if not own and _splits(result):
        found = [c for c in found if not _self_stopped(p, c)]
    found += own
    seen, out = set(), []
    pr = _pr_branch(p, task)
    for c in found:
        k = c.get("marker") or (c["sha"], c["remote"], c["ref"])
        if k not in seen:
            seen.add(k)
            if pr and (c.get("marker") or c["ref"] == f"refs/heads/{pr}"):
                c["pr_branch"] = pr
            out.append(c)
    return out


def _pr_branch(p, task: dict) -> str:
    """The pr_branch task `task` delivers onto, "" when none (local reads only)."""
    from . import push
    try:
        return push.pr_branch_of(p, p.db.task(int(task["id"])) or task)
    except Exception:
        return ""


def _on_pr_head(c: dict, repo, remote: str, ref: str, sha: str, cache: dict) -> tuple[str, str] | None:
    """`sha` claimed on its task's pr_branch (`ref`) that `remote` lacks, confirmed on the open PR's
    head repository when that is a fork (prhead.claim_remote): where `ttp push` sends such a branch,
    and where a push made by hand went. None when it is not such a claim or not there. gh asks within
    the cache's Budget (OutOfTime once it is spent), and the fork's default branch is never read."""
    from . import prhead
    if not c.get("pr_branch") or ref != f"refs/heads/{c['pr_branch']}":
        return None
    fork, _ = prhead.claim_remote(repo, c["pr_branch"], cache.get(Budget.KEY))
    if not fork or fork == remote:
        return None
    state, why = verify(repo, fork, ref, sha, cache)
    return (state, f"{why}, the open PR's head repository") if state == CONFIRMED else None


class OutOfTime(Exception):
    """This tick's Budget is spent: the claim is checked on a later tick, neither failed nor retried."""


class Budget:
    """Seconds of git network calls one daemon tick spends on push claims, kept in a check's cache.
    The tick's first call gets its whole timeout, so each tick makes progress; a later one starts
    only with min(timeout, 1 s) left, its timeout cut to what is left. A cut call that times out is
    OutOfTime, not an unreadable remote. No tick spends more than max(seconds, the longest timeout)."""
    KEY = "budget"   # its key in a cache, whose other keys are tuples

    def __init__(self, seconds: float | None = None) -> None:
        self.seconds = float(NET_BUDGET_S if seconds is None else seconds)
        self.left, self.calls = self.seconds, 0

    def call(self, repo, *args: str, timeout: float) -> subprocess.CompletedProcess:
        return self.run(f"git {args[0]}", timeout, lambda t: _git(repo, *args, timeout=t),
                        lambda r: r.returncode == 124 and "TimeoutExpired" in (r.stderr or ""))

    def run(self, what: str, timeout: float, fn, timed_out):
        """fn(its timeout) under this Budget (a git or gh call); timed_out(its result) tells a timeout."""
        t = min(timeout, self.left) if self.calls else timeout
        if self.calls and self.left < min(timeout, 1.0):
            raise OutOfTime(f"{max(self.left, 0):.0f} s of this tick's {self.seconds:.0f} s left")
        self.calls += 1
        began = time.monotonic()
        try:
            r = fn(t)
        finally:
            self.left -= time.monotonic() - began
        if t < timeout and timed_out(r):
            raise OutOfTime(f"{what} outlasted this tick's {self.seconds:.0f} s")
        return r


def _net(repo, cache: dict, *args: str, timeout: float) -> subprocess.CompletedProcess:
    """A git network call, within the cache's Budget when it has one."""
    b = cache.get(Budget.KEY)
    return b.call(repo, *args, timeout=timeout) if b else _git(repo, *args, timeout=timeout)


def _read_marker(p, name: str) -> dict:
    from . import push
    m = push._read(p.state / push.DETACHED / name)
    return m if isinstance(m, dict) else {}


def _ls(repo, remote: str, ref: str, cache: dict) -> tuple[list[str] | None, str]:
    """The shas `remote` has for `ref` (a tag's peeled commit last), None when it cannot be read."""
    k = (str(repo), remote, ref)
    if k not in cache:
        r = _net(repo, cache, "ls-remote", remote, ref, timeout=LS_TIMEOUT_S)
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()
            cache[k] = (None, f"git ls-remote {remote} failed: {err[-1] if err else f'exit {r.returncode}'}")
        else:
            rows = [ln.split("\t") for ln in r.stdout.splitlines() if "\t" in ln]
            cache[k] = ([s for s, n in rows if n == ref] + [s for s, n in rows if n == f"{ref}^{{}}"], "")
    return cache[k]


def _fetch(repo, remote: str, ref: str, cache: dict) -> str:
    """The tip of remote's branch after a bounded fetch into its tracking ref, "" when it failed."""
    from .push import tracking_ref
    k = ("fetch", str(repo), remote, ref)
    if k not in cache:
        track = tracking_ref(remote, ref[len("refs/heads/"):])
        r = _net(repo, cache, "fetch", "-q", "--no-write-fetch-head", remote, f"+{ref}:{track}", timeout=FETCH_TIMEOUT_S)
        cache[k] = _commit(repo, track) if r.returncode == 0 else ""
    return cache[k]


def verify(repo, remote: str, ref: str, sha: str, cache: dict | None = None) -> tuple[str, str]:
    """(state, why) of `sha` on `remote`'s `ref`: CONFIRMED when it is the tip per ls-remote or
    reachable from it after a fetch; MISSING when the remote has no such ref or the commit is not on
    it; UNKNOWN when the remote cannot be read (the network, a timeout). A MISSING decided by an
    answer an earlier tick read (cache[CARRIED]) is asked again first: the push may have landed since.
    Asked where a push to `remote` goes (push.push_side: its push URL when that is not its fetch URL)."""
    cache = {} if cache is None else cache
    state, why, by = _verify(repo, remote, ref, sha, cache)
    carried = cache.get(CARRIED) or set()
    if state == MISSING and by in carried:
        carried.discard(by)
        cache.pop(by, None)
        state, why, _ = _verify(repo, remote, ref, sha, cache)
    return state, why


def _verify(repo, remote: str, ref: str, sha: str, cache: dict) -> tuple[str, str, tuple]:
    """verify's (state, why, the cache key of the answer that decided it)."""
    from .push import push_side
    where, s = short_ref(remote, ref), sha[:12]
    remote = push_side(repo, remote)
    ls, fetched = (str(repo), remote, ref), ("fetch", str(repo), remote, ref)
    tips, err = _ls(repo, remote, ref, cache)
    if tips is None:
        return UNKNOWN, f"cannot check {s} on {where}: {err}", ls
    if not tips:
        return MISSING, f"{remote} has no {ref} ({s} claimed there)", ls
    tip = tips[-1]
    if tip.startswith(sha) or sha.startswith(tip):
        return CONFIRMED, f"{s} is the tip of {where}", ls
    if ref.startswith("refs/tags/"):
        return MISSING, f"{where} points at {tip[:12]}, not {s}", ls
    after = _fetch(repo, remote, ref, cache)
    if not after:
        return UNKNOWN, f"cannot fetch {where} to look for {s}", fetched
    full = _commit(repo, sha)
    if not full:
        return MISSING, f"{s} is not on {where} (tip {tip[:12]}; no such commit after a fetch)", fetched
    rc = _git(repo, "merge-base", "--is-ancestor", full, after).returncode
    if rc == 0:
        return CONFIRMED, f"{s} is on {where}", fetched
    if rc == 1:
        return MISSING, f"{s} is not on {where} (tip {after[:12]})", fetched
    return UNKNOWN, f"cannot tell whether {s} is on {where} (git merge-base exit {rc})", fetched


def _marker_target(p, m: dict) -> tuple:
    """(repo, (remote, ref) or None) a push marker names: the remote and branch it recorded, else
    its target ("origin/<branch>"), read in its repo or, that worktree pruned since, the project root."""
    repo = m.get("repo") or p.root
    if not Path(repo).is_dir():   # its task's worktree was pruned since
        repo = p.root
    remote, branch, target = str(m.get("remote") or ""), str(m.get("branch") or ""), str(m.get("target") or "")
    to = ((remote, f"refs/heads/{branch}") if remote and branch and remote in _remotes(repo)
          else _ref(repo, target) if target else None)
    return repo, to


def _marker_local(p, c: dict, state: str, why: str) -> tuple[str, str]:
    """A marker claim the remote check left UNKNOWN or UNVERIFIABLE, confirmed when its commit is an
    ancestor of the target's local tracking ref (a later commit on the branch is fine). Local refs
    only, never a fetch; a MISSING the remote answered stands."""
    if state not in (UNKNOWN, UNVERIFIABLE):
        return state, why
    try:
        m = _read_marker(p, c["marker"])
        if m.get("status") in ("queued", "running"):
            return state, why
        sha = str((m.get("sha") if m.get("status") == "pushed" else m.get("head")) or "")
        repo, to = _marker_target(p, m)
        if not sha or not to or not to[1].startswith("refs/heads/"):
            return state, why
        tracking = f"refs/remotes/{to[0]}/{to[1][len('refs/heads/'):]}"
        full, tip = _commit(repo, sha), _commit(repo, tracking)
        if full and tip and _git(repo, "merge-base", "--is-ancestor", full, tip).returncode == 0:
            return CONFIRMED, f"{sha[:12]} is on {short_ref(*to)} per its local tracking ref ({why})"
    except Exception:
        pass
    return state, why


def _check_local(p, c: dict, m: dict) -> tuple[str, str] | None:
    """An own push in a code repo with no git remote (push.LOCAL) fast-forwarded its local branch:
    its claimed commit is checked against refs/heads/<branch> there, no network. None when the marker
    is not such a push. Accepted gap: summary lines and `pushed` entries in such a repo stay unverified."""
    from . import push
    if str(m.get("remote") or "") != push.LOCAL:
        return None
    repo = m.get("repo") or p.root
    if not Path(repo).is_dir():   # its task's worktree was pruned since
        repo = p.root
    if not push.remote_less(Path(repo)):
        return None
    branch = str(m.get("branch") or "")
    sha = str((m.get("sha") if m.get("status") == "pushed" else m.get("head")) or "")
    where, s = f"local branch {branch or '?'}", sha[:12] or "?"
    tip = _commit(repo, f"refs/heads/{branch}") if branch else ""
    if not tip:
        return MISSING, f"{c['source']} claimed {s} on {where}, which does not exist"
    full = _commit(repo, sha) if sha else ""
    if full and _git(repo, "merge-base", "--is-ancestor", full, tip).returncode == 0:
        return CONFIRMED, f"{s} is {'the tip of' if full == tip else 'on'} {where}"
    return MISSING, f"{s} is not on {where} (tip {tip[:12]}; claimed by {c['source']})"


def _check_marker(p, c: dict, cache: dict) -> tuple[str, str]:
    m = _read_marker(p, c["marker"])
    status, target = m.get("status"), str(m.get("target") or "")
    if status in ("queued", "running"):
        return UNKNOWN, f"{c['source']} is still {status}"
    if local := _check_local(p, c, m):
        return local
    repo, to = _marker_target(p, m)
    if not to:
        return UNVERIFIABLE, f"{c['source']} names no remote branch to check ({target or 'no target'})"
    sha = str(m.get("sha") or "") if status == "pushed" else ""
    if sha:
        return verify(repo, to[0], to[1], sha, cache)
    head = str(m.get("head") or "")
    why = str(m.get("reason") or m.get("message") or (f"exit {m['exit']}" if m.get("exit") is not None else "")
              or "no outcome")
    if head:
        state, said = verify(repo, to[0], to[1], head, cache)
        if state == MISSING and (fork := _on_pr_head(c, repo, to[0], to[1], head, cache)):
            state, said = fork
        if state == CONFIRMED:
            return state, f"{said} (pushed another way after {c['source']} ended {status})"
        if state == UNKNOWN:
            return state, said
    return (MISSING if m.get("exit") is not None else UNVERIFIABLE), (
        f"{c['source']} ended {status or 'without an outcome'} ({why[:200]}) and "
        f"{(head or '?')[:12]} is not on {short_ref(*to)}")


def check(p, c: dict, cache: dict) -> tuple[str, str]:
    """One claim's (state, why); a summary's claim that names a ref the remote lacks also counts as
    pushed when its commit is on the project's push branch (the line may name a branch loosely)."""
    if c.get("marker"):
        return _check_marker(p, c, cache)
    repo = c.get("repo") or p.root
    state, why = verify(repo, c["remote"], c["ref"], c["sha"], cache)
    if state == MISSING and (head := _on_pr_head(c, repo, c["remote"], c["ref"], c["sha"], cache)):
        return head[0], f"{head[1]} ({why})"
    if state == MISSING and not c.get("strict") and (d := _default(p, repo)) and d != (c["remote"], c["ref"]):
        s2, w2 = verify(repo, d[0], d[1], c["sha"], cache)
        if s2 == CONFIRMED:
            return s2, f"{w2} ({why})"
    return state, why


def check_all(p, cs: list[dict], cache: dict | None = None) -> tuple[str, str, list[tuple[dict, str, str]]]:
    """(overall state, why, each claim's) of claims `cs`: MISSING if any is missing, else UNVERIFIABLE
    if any is, else UNKNOWN if any cannot be told yet, else CONFIRMED. OutOfTime passes through: nothing was told."""
    cache = {} if cache is None else cache
    each = []
    for c in cs:
        try:
            state, why = check(p, c, cache)
        except OutOfTime:
            raise
        except Exception as e:   # an unreadable claim is not a verified one, nor a missing one
            state, why = UNKNOWN, f"could not check {c.get('source')}: {type(e).__name__}: {e}"
        each.append((c, state, why))
    for want in (MISSING, UNVERIFIABLE, UNKNOWN):
        bad = [w for _, s, w in each if s == want]
        if bad:
            return want, "; ".join(bad), each
    return CONFIRMED, "; ".join(w for _, _, w in each), each


def delay_s(tries: int) -> float:
    return float(BACKOFF_S[min(max(tries, 0), len(BACKOFF_S) - 1)])


def describe(cs: list[dict]) -> str:
    parts = [c["source"] if c.get("marker") else f"{c['sha'][:12]} to {short_ref(c['remote'], c['ref'])}"
             for c in cs]
    return ", ".join(parts[:5]) + (f" and {len(parts) - 5} more" if len(parts) > 5 else "")


# The audit ------------------------------------------------------------------------------------------

def _last_run(db, tid: int):
    return db.one("SELECT * FROM runs WHERE task=? AND role='worker' ORDER BY id DESC LIMIT 1", (tid,))


def _on_pass_gap(prev: dict, run) -> str:
    """Why a done recorded from a waiting hand-off skipped the on_pass_cmd it named, else ""."""
    if not run or not run["dir"] or prev.get("run_status") != "model-free":
        return ""
    d = Path(run["dir"])
    try:
        waited = json.loads((d / "result.json").read_text())
    except (OSError, ValueError):
        return ""
    cmd = waited.get("on_pass_cmd") if isinstance(waited, dict) else None
    if isinstance(cmd, str) and cmd.strip() and not (d / ON_PASS_DIR).is_dir():
        return f"done recorded from its on_pass while its on_pass_cmd never ran: {cmd.strip()[:200]}"
    return ""


def _contains(repo, sha: str, branch: str) -> bool:
    full = _commit(repo, sha)
    return bool(full) and _git(repo, "merge-base", "--is-ancestor", full, f"refs/heads/{branch}").returncode == 0


def _reattribute(p, t, c: dict, state: str, why: str, claimed: dict, tips: dict) -> tuple[str, str]:
    """A claim the remote check did not confirm, decided again as `ttp landed` does: CONFIRMED when the
    commit (or one recorded as superseding the task) is on the push branch by ancestry, SHARED when
    another task's branch holds the commit and this task's does not.

    Local refs only, never a fetch: the push branch's tip is read once per repo per audit (`tips`)
    from its tracking ref as the remote checks (within the tick's Budget) or an earlier fetch left it.
    Accepted gap: a commit the local ref has not seen yet stays unconfirmed until an audit after
    the next fetch."""
    from . import landed
    repo = Path(c.get("repo") or p.root)
    try:
        if repo not in tips:
            tips[repo] = landed._tip(repo, landed._push_ref(p, repo), fetch=False)[0]
        tip = tips[repo]
        shas = [c["sha"]] + [r["sha"] for r in p.db.q("SELECT sha FROM task_superseded WHERE task=?", (t["id"],))]
        for s in shas:
            full = _commit(repo, s)
            if tip and full and landed.on_branch(repo, full, tip):
                return CONFIRMED, f"{s[:12]} is on the push branch"
        others = [o for o in claimed.get(c["sha"][:12], []) if o["id"] != t["id"]]
        if others and not (t["branch"] and _contains(repo, c["sha"], t["branch"])):
            owner = next((o for o in others if o["branch"] and _contains(repo, c["sha"], o["branch"])), None)
            if owner:
                return SHARED, f"shared claim: {c['sha'][:12]} is #{owner['id']}'s commit (its branch holds it)"
    except Exception:
        pass
    return state, why


def audit(p, days: float = AUDIT_DAYS, now: float | None = None, max_refs: int = AUDIT_REFS,
          cache: dict | None = None) -> dict:
    """Done tasks of the last `days` whose claimed pushes are not on the remote: {"counts": {state: n},
    "gaps": [{"task", "title", "state", "why"}], "tasks": n, "landed": [ids of the tasks that claim a
    push, every one confirmed]}. Read-only, apart from fetching the
    remote's tracking refs. Past `max_refs` distinct refs the rest count as unknown. `cache`: the
    remote's answers so far (with a Budget, an audit that ran out of time resumes from them)."""
    from .db import NOT_ABANDONED_SQL, load_result
    now = time.time() if now is None else now
    db = p.db
    rows = db.q(f"SELECT id, title, result, branch FROM tasks WHERE status='done' AND updated>=? AND {NOT_ABANDONED_SQL} "
                "ORDER BY id",
                (now - days * 86400,))
    counts = {CONFIRMED: 0, MISSING: 0, UNKNOWN: 0}
    gaps: list[dict] = []
    cache = {} if cache is None else cache
    refs: set = set()
    tips: dict = {}                    # repo -> the push branch's local tip, read once
    claimed: dict[str, list] = {}      # sha -> the tasks claiming it (for a shared claim)
    todo, landed = [], []
    for t in rows:
        prev = load_result(t["result"])
        run = _last_run(db, t["id"])
        cs = queue_claims(p, t["id"])
        if not t["branch"] and not cs:
            # Nothing of its own to push and no landing the queue recorded under it (a review that
            # approved one has no branch but owns its queue claims). Accepted gap: a task that has a
            # branch but makes no commits (a question or review kind) and claims a push only in its
            # summary is still checked, and can be reported.
            continue
        try:
            cs += [c for c in claims(p, {"id": t["id"]}, prev, dict(run) if run else None)
                   if c.get("marker") or not any(q["sha"].startswith(c["sha"][:12]) or c["sha"].startswith(q["sha"])
                                                 for q in cs)]
        except Exception as e:
            gaps.append({"task": t["id"], "title": t["title"], "state": UNKNOWN,
                         "why": f"could not read its claims: {type(e).__name__}: {e}"})
            counts[UNKNOWN] += 1
            continue
        for c in cs:
            if c.get("sha"):
                claimed.setdefault(c["sha"][:12], []).append(t)
        todo.append((t, prev, run, cs))
    for t, prev, run, cs in todo:
        all_ok = bool(cs)
        for c in cs:
            k = c.get("marker") or (c["remote"], c["ref"])
            over = k not in refs and len(refs) >= max_refs
            if over:
                state, why = UNKNOWN, f"not checked: more than {max_refs} refs in one audit"
            else:
                refs.add(k)
                _, state, why = check_all(p, [c], cache)[2][0]
            if state != CONFIRMED and not over and c.get("sha") and not c.get("marker"):
                state, why = _reattribute(p, t, c, state, why, claimed, tips)
            elif state != CONFIRMED and not over and c.get("marker"):
                state, why = _marker_local(p, c, state, why)
            if state in (SHARED, UNVERIFIABLE):   # counted apart, only when there are any
                counts[state] = counts.get(state, 0) + 1
            else:
                counts[state] += 1
            if state != CONFIRMED:
                all_ok = False
                gaps.append({"task": t["id"], "title": t["title"], "state": state, "why": why,
                             "sha": c.get("sha", ""), "source": c.get("source", "")})
        why = "" if all_ok else _on_pass_gap(prev, run)    # claims the remote has: the on_pass_cmd's work is there
        if all_ok:
            landed.append(t["id"])
        if why:
            counts[MISSING] += 1
            gaps.append({"task": t["id"], "title": t["title"], "state": MISSING, "why": why, "sha": "",
                         "source": "on_pass"})
    return {"at": now, "days": days, "tasks": len(rows), "counts": counts, "gaps": gaps, "landed": landed}


def gap_key(g: dict) -> str:
    return f"push-gap:{g['task']}:{(g.get('sha') or g.get('source') or '')[:12]}"


def gap_text(g: dict) -> str:
    return (f"#{g['task']} {str(g.get('title') or '')[:80]} is done, but its claimed push is not on the remote: "
            f"{g['why']}. Requeue it to push, or record where its work landed.")


def summary_line(a: dict) -> str:
    c = a.get("counts") or {}
    days = a.get("days", AUDIT_DAYS)
    line = (f"{c.get(CONFIRMED, 0)} confirmed, {c.get(MISSING, 0)} missing, {c.get(UNKNOWN, 0)} unknown "
            f"across {a.get('tasks', 0)} done tasks of the last {days:g} d")
    if c.get(UNVERIFIABLE):
        line += f"; {c[UNVERIFIABLE]} unverifiable"
    missing = [g for g in a.get("gaps") or [] if g.get("state") == MISSING]
    if missing:
        line += "; missing: " + "; ".join(f"#{g['task']} ({g['why'][:160]})" for g in missing[:5])
        if len(missing) > 5:
            line += f" and {len(missing) - 5} more"
    return line


def daily_line(db) -> str:
    a = db.kv(AUDIT_KV)
    if not isinstance(a, dict) or not a.get("at"):
        return "not run yet"
    return summary_line(a)
