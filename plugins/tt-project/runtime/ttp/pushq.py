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

from . import checkdirs, batch, conflictfix, coordinator as coord, cyclenotice, envfault, locks, msgparts, prhead, push, pushclaim, replymsg, runner, scopehold, sharedchecks, timefmt, upstream
from .db import (TERMINAL_TASK_STATES, BranchIndex, continues_id, dependency_ids, dump_result, load_result,
                 pin_of, review_subject, reviewed_ids)
from .landed import on_branch
from .project import Project, nice_level, push_allowed, push_queue_on, renice, write_json
from .worktree import fix_branches, named_refs

REF_PREFIX = "refs/ttp/push/"   # + row id: pins the approved commit until its row is settled
KV = "push_queue"               # kv: {"backoff_until", "deaths", "bad_tip": {"tip", "fails", "tried", "at", ...}, "tips_told"}
DEFAULT_BATCH_S = 900           # delivery.push_batch_s: the oldest approval waits at most this long
DEFAULT_BATCH_MAX = 8           # delivery.push_batch_max: start at once with this many; also a batch's most
DEFAULT_MIN_GAP_S = 1800        # delivery.push_min_gap_s: the least time from a batch's end to the next start
# delivery.push_solo_paths: git pathspecs; a batch takes at most one approval whose change touches them.
# Two prompt changes that each fit a size cap may not fit together, which fails the whole batch.
TRIM_EVENT = "its review runs again as a light trim-to-fit fix"   # counts them per review
SOLO_PATHS = ("plugins/tt-project/template/prompts/*.md",)
HOLD_S = 1800                   # after tip_failed, approvals already tried on that tip wait this long unless it changes
TIP_FAILS = 2                   # tip_failed batches in a row on one tip before it is known bad: alerted, never checked alone
BAD_TIP_RETRY_S = 6 * 3600      # a known-bad tip with nothing new to try: the tried approvals run again after this
BAD_TIP_ALERT = "push_tip_failing"   # the keyed alert while the push branch tip is known bad (alerts.holds)
BACKOFF_STEP_S = 300            # a dead batch's rows wait 5 min per try ...
BACKOFF_MAX_S = 1800            # ... and at most 30 min
DYING_AFTER = 3                 # batches that die in a row before push_queue_dying is raised
DEFAULT_LANDING_ALERT_H = 4     # delivery.push_landing_alert_h: approvals waiting this long with no landing alert (high)
LANDING_ALERT = "push_not_landing"   # the keyed alert while nothing lands despite batches (alerts.holds)
LANDED_OUTCOMES = ("pushed", "landed")   # batch outcomes that put the approvals on the push branch
DEPLOY_ALERTED = "deploy_alerted"   # deploy.ALERTED: the last deploy alert, by commit
RERUN_START_S = 900              # a receipt's rerun that has not taken it up in this long never started
MAX_RESUMES = 1                 # respawns of an after_push that died (a reboot killed the deploy)
RECOVER_S = 1800                # a batch that died mid-push: how long its remote may stay unreadable
RECOVER_GAP_S = 120             # ... asked at most this often meanwhile
RECOVER_FETCH_S = 20            # ... each fetch bounded, as the tick waits for it
REFUSAL_TIMEOUT_S = 30          # the approval asks the remote for its default branch; never hang the tick
DEFAULT_BRANCH_TTL_S = 3600     # ... and remembers the answer per remote this long (one ask, not one per approval)
DEFAULT_BRANCH_RETRY_S = 300    # an unreachable remote is asked again after this, not on every approval
DEFAULT_BRANCH_KV = "default_branch:"   # + remote: {"until", "branch"}, so a restart does not start cold
ABSENT_TTL_S = 300              # _absent's answer, asked before a transaction, is read inside it this long
AFTER_PUSH_OFF = "after_push_off"   # kv: true while after_push is unset (alerts.holds)
AFTER_PUSH_OK = "after_push_ok_at"  # kv: when an ordinary push's after_push last succeeded (alerts.holds)
TIPS_TOLD = 20                  # tip_failed shas remembered, so each tip is reported once
FLAKY_EVENT = "push_flaky_test"   # a batch's failing tests passed on a rerun at the same head (batch._flaky)
FLAKY_TOLD_S = 86400.0          # a flaky test id is reported once in this long ("flaky_told")
ROW_RESULTS = ("pushed", "landed", "conflict", "check_failed", "requeued", "refused", "scope_held")
AFTER_STATES = ("ok", "failed", "timeout", "killed", "skipped")
STATS_S = 7 * 86400            # the conflict counts `ttp push --queue` and the web app show cover this long
INHERIT_WINDOW_S = 30 * 86400  # reviews finished this long ago at most tell which branches were rejected
INHERIT_GIT_S = 30             # each git call of that check, bounded, as the tick waits for it
RESOLVES_MAX = 20               # upstream note fingerprints one approval may resolve (a hand-off's `resolves`)
_HEX40 = re.compile(r"[0-9a-f]{40}")
REBASE_LABEL = "push_rebase:"   # + the conflicting head: a review the daemon queued to rebase it (auto_rebase)
_children: dict[str, subprocess.Popen] = {}   # batch processes this daemon started, reaped by finalize
_default_branches: dict[tuple[str, str], tuple[float, str | None]] = {}   # (root, remote): (until, branch)
_recover_asked: dict[str, float] = {}         # batch id: when its remote was last asked (_recover)
_absent_seen: dict[tuple[str, str, str], tuple[float, bool, str]] = {}   # (root, remote, branch): (until, absent, why)


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


def _asks(p: Project, ask: bool | None) -> bool:
    """Whether a lookup may ask the remote: never while a write transaction is open (it would hold the
    lock as long as the network takes), so it reads the answer prefetch left before it opened."""
    return (not p.db.conn.in_transaction) if ask is None else ask


def _default_branch(p: Project, remote: str, now: float | None = None, ask: bool | None = None) -> str | None:
    """The remote's default branch, None when unknown. One `git ls-remote` (up to REFUSAL_TIMEOUT_S)
    per remote and DEFAULT_BRANCH_TTL_S (DEFAULT_BRANCH_RETRY_S while it fails): many approvals in
    one tick must not each wait on the network. The answer is kept in the project's kv too, so a
    restarted daemon does not ask again. Inside a transaction (see _asks) only the kept answer counts."""
    now = time.time() if now is None else now
    key = (str(p.root), remote)
    hit = _default_branches.get(key)
    if not (hit and hit[0] > now):
        kept = p.db.kv(DEFAULT_BRANCH_KV + remote)
        if isinstance(kept, dict) and isinstance(kept.get("until"), (int, float)):
            hit = _default_branches[key] = (kept["until"], kept.get("branch"))
    if hit and hit[0] > now:
        return hit[1]
    if not _asks(p, ask):
        return None   # unknown: as an unreachable remote, it refuses nothing (the batch asks again)
    try:
        ls = _git(p, "ls-remote", "--symref", remote, "HEAD", timeout=REFUSAL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        ls = None
    found = None
    if ls is not None and ls.returncode == 0:
        found = ""
        for line in ls.stdout.splitlines():
            ref = line[5:].split("\t")[0] if line.startswith("ref: ") else ""
            if ref.startswith("refs/heads/"):
                found = ref[len("refs/heads/"):]
                break
    until = now + (DEFAULT_BRANCH_RETRY_S if found is None else DEFAULT_BRANCH_TTL_S)
    _default_branches[key] = (until, found)
    p.db.set_kv(DEFAULT_BRANCH_KV + remote, {"until": until, "branch": found})
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


def _flaky_told(db, st: dict, b, m: dict, now: float) -> None:
    """The marker's flaky_tests (tests that failed and then passed on one rerun at the same head) and
    pre_existing (tests that failed on a rerun on the tip alone too): one queued FLAKY_EVENT each naming the
    ids not reported in the last FLAKY_TOLD_S, so the coordinator queues a fix that makes them deterministic."""
    flaky = [f for f in m.get("flaky_tests") or [] if isinstance(f, dict)]
    told = {k: v for k, v in (st.get("flaky_told") or {}).items() if isinstance(v, (int, float)) and now - v < FLAKY_TOLD_S}
    new = sorted({str(i) for f in flaky for i in f.get("ids") or []} - set(told))
    if new:
        told.update({i: now for i in new})
        heads = sorted({_short(str(f.get("head") or "")) for f in flaky if set(map(str, f.get("ids") or [])) & set(new)})
        _event(db, None, FLAKY_EVENT,
               f"flaky test{'s' if len(new) > 1 else ''} in push batch {b['id']}: {', '.join(new)} failed, then passed on "
               f"one rerun at the same head ({', '.join(heads)}), so the batch went on. Queue a fix that makes "
               f"{'them' if len(new) > 1 else 'it'} deterministic (wait on the real condition or a test clock, not "
               f"wall time).", queued=True)
    pre = [f for f in m.get("pre_existing") or [] if isinstance(f, dict)]
    new = sorted({str(i) for f in pre for i in f.get("ids") or []} - set(told))
    if new:
        told.update({i: now for i in new})
        tips = sorted({_short(str(f.get("tip") or "")) for f in pre if set(map(str, f.get("ids") or [])) & set(new)})
        entries = sorted({str(f.get("entry")) for f in pre if set(map(str, f.get("ids") or [])) & set(new)})
        _event(db, None, FLAKY_EVENT,
               f"test{'s' if len(new) > 1 else ''} failing on the push branch itself in push batch {b['id']}: "
               f"{', '.join(new)} failed with entr{'y' if len(entries) == 1 else 'ies'} {', '.join(entries)} and "
               f"again on a rerun on the tip ({', '.join(tips)}) alone: pre-existing (flaky or already failing), "
               f"so no entry was sent back for {'them' if len(new) > 1 else 'it'}. Queue a fix that makes "
               f"{'them' if len(new) > 1 else 'it'} pass reliably.", queued=True)
    if told or "flaky_told" in st:
        st["flaky_told"] = told


def _reply(db, task: dict, new: str, text: str, need: str | None = None) -> None:
    """The answer to the chat that asked for the task, as Daemon._finish_worker gives it."""
    if not task["reply_chat"] or new not in ("done", "failed", "blocked"):
        return
    text = text if new == "done" else f"(task #{task['id']} {new}) {text}"
    if need and need not in text:
        text += f"\nNeeds from you: {need}"
    msgparts.post(db, msgparts.cut(text, f"task #{task['id']}'s hand-off"),
                  chat=None if task["reply_chat"] == "all" else task["reply_chat"], kind="reply", severity="normal")


def _done_text(prev: dict, summary: str, tail: str) -> str:
    """A landed task's reply: its hand-off's `message` (replymsg) with how it landed, else its summary."""
    msg = prev.get(replymsg.FIELD)
    return f"{msg.strip()} ({tail})" if isinstance(msg, str) and msg.strip() else summary


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


def target_line(db, tz: str | None = None) -> str | None:
    """The daily review's line on how far the intended target (delivery.base_ref) is behind the push
    branch, from the reach the last pushed batch recorded in its marker: no git, no model. None when
    no batch recorded a reach (the branches are the same, no marker, or nothing pushed yet)."""
    row = db.one("SELECT id, target, ended, started FROM push_batches WHERE outcome IN ('pushed','landed') "
                 "ORDER BY started DESC LIMIT 1")
    r = _reach(db, row["id"]) if row else None
    if not r or not r.get("ref") or r["ref"] == row["target"]:
        return None
    if tz:   # the project's home zone
        when = timefmt.long(row["ended"] or row["started"], tz)
    else:
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


def _verdicts(db, review: dict, branches: set[str], now: float) -> tuple[dict[int, int], set[int], dict[int, str]]:
    """Per code task, the latest finished review covering it (see _covers) within INHERIT_WINDOW_S,
    leaving out `review` itself and the tasks it covers: ({failed task: its review}, {passed tasks},
    {task: branch})."""
    code = db.q("SELECT id, branch, labels FROM tasks WHERE kind='code' AND branch IS NOT NULL AND branch!=''")
    index = BranchIndex(code)
    own = _covers(db, review, [{"branch": b} for b in branches], index)
    reviews = db.q("SELECT * FROM tasks WHERE kind='review' AND status IN ('done','pushing','failed') AND id!=? "
                   "AND updated>=? ORDER BY id DESC", (review["id"], now - INHERIT_WINDOW_S))
    rows: dict[int, list[dict]] = {}
    for r in db.q("SELECT task, branch FROM push_queue WHERE task IN (%s)" % ",".join("?" * len(reviews)),
                  [r["id"] for r in reviews]) if reviews else []:
        rows.setdefault(r["task"], []).append(r)
    failed, passed, seen = {}, set(), set(own)
    for r in reviews:
        for tid in _covers(db, r, rows.get(r["id"], []), index) - seen:
            seen.add(tid)
            if r["status"] == "failed":
                failed[tid] = r["id"]
            else:
                passed.add(tid)
    return failed, passed, {t["id"]: t["branch"] for t in code if t["branch"] not in branches}


def _patch_ids(p: Project, shas: list[str]) -> dict[str, str]:
    """{patch-id: commit} of `shas` (git patch-id --stable): equal for a rebased copy of a commit."""
    if not shas:
        return {}
    show = _git(p, "show", "--no-color", "--format=commit %H", "-p", *shas, timeout=INHERIT_GIT_S)
    pid = subprocess.run(["git", "-C", str(p.root), "patch-id", "--stable"], input=show.stdout, text=True,
                         capture_output=True, timeout=INHERIT_GIT_S)
    return {f[0]: f[1] for f in (line.split() for line in pid.stdout.splitlines()) if len(f) == 2}


def _inherited(p: Project, task: dict, entries: list[dict], tip: str, now: float) -> dict[str, dict[int, list[str]]]:
    """Commits each head adds over the target's `tip` that come from another task whose latest review
    failed: {head: {task id: ["<short sha> <subject>", ...]}}. A commit counts by its hash or by its
    patch-id, so a rebased or cherry-picked copy of a rejected commit is caught too. A rejected
    branch's commits that a passed task's branch also holds are that task's (it was stacked on it)."""
    failed, passed, branch_of = _verdicts(p.db, task, {e["branch"] for e in entries if e["branch"]}, now)
    have = set(_git(p, "for-each-ref", "--format=%(refname:short)", "refs/heads/",
                    timeout=INHERIT_GIT_S).stdout.split())
    bad = {branch_of[t]: t for t in failed if branch_of.get(t) in have}
    if not bad:
        return {}
    good = [branch_of[t] for t in passed if branch_of.get(t) in have]
    rejected = _git(p, "rev-list", "--no-merges", *bad, "--not", tip, *good, timeout=INHERIT_GIT_S).stdout.split()
    if not rejected:
        return {}
    rejected_set, rejected_ids = set(rejected), _patch_ids(p, rejected)
    out: dict[str, dict[int, list[str]]] = {}
    for e in entries:
        adds = _git(p, "rev-list", "--no-merges", "--right-only", "--cherry-pick", f"{tip}...{e['head']}",
                    timeout=INHERIT_GIT_S).stdout.split()
        hits = {c: c for c in adds if c in rejected_set}
        rest = [c for c in adds if c not in hits]
        hits |= {c: rejected_ids[i] for i, c in _patch_ids(p, rest).items() if i in rejected_ids}
        for c, src in hits.items():
            holders = _git(p, "for-each-ref", "--contains", src, "--format=%(refname:short)", "refs/heads/",
                           timeout=INHERIT_GIT_S).stdout.split()
            subject = _git(p, "log", "-1", "--format=%h %s", c, timeout=INHERIT_GIT_S).stdout.strip()
            for tid in sorted({bad[b] for b in holders if b in bad}):
                out.setdefault(e["head"], {}).setdefault(tid, []).append(subject)
    return out


def carried(p: Project, task: dict, head: str, cfg: dict | None = None) -> dict[int, list[str]]:
    """What check_approval would refuse in a head the daemon is about to queue a review of: {task id:
    ["<short sha> <subject>", ...]} of the commits `head` carries from other tasks whose latest review
    failed (see _inherited), judged as the auto review of code task `task` will be. {} when the queue
    is off, the target was never fetched, or git fails or hangs: the approval still checks it."""
    tgt = target(p) if enabled(p, cfg) else None
    tip = _tracking_tip(p, *tgt) if tgt else ""
    if not tip or not task.get("branch"):
        return {}
    review = {"id": -1, "parent": None, "title": "", "spec": "", "depends_on": json.dumps([task["id"]]),
              "labels": json.dumps([f"auto_review:{task['id']}"])}
    try:
        return _inherited(p, review, [{"branch": task["branch"], "head": head}], tip, time.time()).get(head, {})
    except (subprocess.SubprocessError, OSError):
        return {}


def check_approval(p: Project, task: dict, entries: Any, cfg: dict | None = None) -> dict:
    """Validate a review's `push` list with fast local git: {"ignored": why} in a project that does
    not push or, while the queue is off, for anything but a list of approvals; {"invalid": why} (also
    for such a list while the queue is off: turned off while the review ran, it runs again and
    pushes itself, as queue_off sends back approvals made before), or {"entries": [{"branch", "head"}], "target": "remote/branch", "added": {head:
    [commit lines]}}. Each head is a full hash of a commit in the project root that the review
    reviewed (worktree.named_refs) or an ancestor of one, or a commit of a fix branch of the lineage
    (worktree.fix_branches: a re-plan that committed on its own branch, not the reviewed one) that
    contains the reviewed branch it builds on. Only the commit the review names is pinned and
    pushed, so a head the reviewer did not name never goes out. A re-approval after a push conflict
    may name any commit; the commits it adds over the conflicting head are listed for the event. A
    head that carries commits of another task whose latest review failed (see _inherited) is invalid
    unless its entry lists that task under "inherited": the review judged them; "inherited" then
    names them per head for the event. A reviewed task whose pr_branch (push.pr_elsewhere) the push
    remote has no branch of is a push-branch delivery: "notes" says its label was ignored and why."""
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
    notes = []
    for t in push._reviewed(p, str(task["id"])):
        if onto := push.pr_elsewhere(p, t, d):
            if tgt and _absent(p, tgt[0], onto):
                notes.append(f"#{t['id']}'s pr_branch label {onto} was ignored: {tgt[0]} has no such branch"
                             f"{_fork_why(p, tgt[0], onto)}, so the review's approval delivers it to the push branch")
                continue
            return {"invalid": f"#{t['id']} delivers onto its pr_branch {onto}, not the push branch"
                               f"{_fork_why(p, tgt[0], onto) if tgt else ''}: publish it there with "
                               f"`ttp push --own --detach` instead of approving it for the push queue"}
    if not tgt:
        return {"invalid": "no target branch: set delivery.push_branch"}
    why = _refusal(p, *tgt, push.allow_protected(d))
    if why:
        return {"invalid": why}
    if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
        return {"invalid": 'list the approved commits as "push": [{"branch": ..., "head": <40-hex sha>}]'}
    rows = p.db.q("SELECT status, head, created FROM push_queue WHERE task=? ORDER BY id", (task["id"],))
    conflicted = [r["head"] for r in rows if r["created"] == rows[-1]["created"] and r["status"] == "conflict"]
    if not rows and (h := rebase_head(task)):
        conflicted = [h]   # a rebase review (auto_rebase) re-approves the head its predecessor could not land
    reviewed: list[str] | None = None
    built: list[tuple[str, str]] = []
    out, added, accepted = [], {}, {}
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
                refs = named_refs(p, task)
                fixes = fix_branches(p, task, refs)
                reviewed = [c for c in (_commit(p, r) for r in refs) if c]
                built = [(tip, on) for tip, on in ((_commit(p, b), _commit(p, r)) for b, r in fixes.items())
                         if tip and on]
                names = ", ".join(refs + list(fixes)) or "none"
            if not _contains(p, reviewed, head) and \
                    not any(_contains(p, [tip], head) and _contains(p, [head], on) for tip, on in built):
                return {"invalid": f"{head} is none of the reviewed refs ({names}) nor an ancestor of one"}
        out.append({"branch": str(e.get("branch") or "").strip()[:200], "head": head})
        ids = e.get("inherited") if isinstance(e.get("inherited"), list) else []
        accepted[head] = {int(str(i).lstrip("#")) for i in ids if str(i).lstrip("#").isdigit()}
    tip = _tracking_tip(p, *tgt)
    try:
        carried = _inherited(p, task, out, tip, time.time()) if tip else {}
    except subprocess.TimeoutExpired:
        carried = {}   # git hung: the push checks still run; the tick must not wait on it
    taken: dict[str, list[int]] = {}
    for head, by_task in carried.items():
        left = {t: c for t, c in by_task.items() if t not in accepted[head]}
        if left:
            tid, commits = next(iter(left.items()))
            more = f" (and from #{', #'.join(map(str, list(left)[1:]))})" if len(left) > 1 else ""
            return {"invalid": f"{_short(head)} carries unreviewed commits from #{tid}{more}, whose latest review "
                               f"failed: {'; '.join(commits[:10])}. Approve a head without them, or judge them in "
                               f"this review and, if they are sound, add \"inherited\": {sorted(left)} to its entry"}
        taken[head] = sorted(by_task)
    return {"entries": out, "target": f"{tgt[0]}/{tgt[1]}", "added": added,
            **({"inherited": taken} if taken else {}), **({"notes": notes} if notes else {})}


def _absent(p: Project, remote: str, branch: str, ask: bool | None = None) -> bool:
    """Whether `remote` answers that it has no branch `branch` (one bounded `git ls-remote`), nor has
    the open PR's head repository when that is a fork (_on_fork). An unreachable remote is not an
    answer: False, so the approval is refused as before. Inside a transaction (see _asks) it reads the
    answer prefetch got within ABSENT_TTL_S, else False."""
    key, now = (str(p.root), remote, branch), time.time()
    if not _asks(p, ask):
        hit = _absent_seen.get(key)
        return bool(hit and hit[0] > now and hit[1])
    try:
        ls = _git(p, "ls-remote", "--heads", push.push_side(p.root, remote), f"refs/heads/{branch}",
                  timeout=REFUSAL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    if ls.returncode != 0:
        return False
    absent, why = not any(ln.endswith(f"\trefs/heads/{branch}") for ln in ls.stdout.splitlines()), ""
    if absent:
        on, why = _on_fork(p, remote, branch)
        if on is None:
            return False
        absent = not on
    _absent_seen[key] = (now + ABSENT_TTL_S, absent, why)
    return absent


def _on_fork(p: Project, remote: str, branch: str) -> tuple[bool | None, str]:
    """(whether the open PR's head repository has `branch`, why) for a branch `remote` lacks: the fork
    remote `ttp push --own` publishes it to (prhead.claim_remote). False with the reason, so `remote`'s
    answer stands, when the PR is not on a fork, there is none or gh cannot answer; None when the fork
    cannot be read in time (no answer, as an unreachable remote). gh and the fork's `git ls-remote`
    share one REFUSAL_TIMEOUT_S Budget; the fork's default branch is never read."""
    b = pushclaim.Budget(REFUSAL_TIMEOUT_S)
    try:
        fork, why = prhead.claim_remote(p.root, branch, b, remote)
        if not fork:
            return False, why or f"the open PR for {branch} has its head on {remote}'s repository"
        ls = b.call(p.root, "ls-remote", "--heads", fork, f"refs/heads/{branch}", timeout=REFUSAL_TIMEOUT_S)
    except pushclaim.OutOfTime as e:
        return None, f"the open PR's head repository could not be read in time ({e})"
    if ls.returncode != 0:
        return None, f"remote {fork}, the open PR's head repository, could not be read"
    on = any(ln.endswith(f"\trefs/heads/{branch}") for ln in ls.stdout.splitlines())
    return on, f"{why}: {branch} is on remote {fork}" if on else f"{why}: remote {fork} has no {branch} either"


def _fork_why(p: Project, remote: str, branch: str) -> str:
    """What _absent found on the open PR's head repository for `branch`, as " (...)", or ""."""
    hit = _absent_seen.get((str(p.root), remote, branch))
    return f" ({hit[2]})" if hit and len(hit) > 2 and hit[2] else ""


def prefetch(p: Project, task: dict, cfg: dict | None = None) -> None:
    """Asks the remote, before the transaction that finishes `task`'s run opens, what deciding its
    hand-off reads there: the default branch (a review's approval: _refusal) and whether each
    pr_branch label names a branch the remote lacks (check_approval, Daemon._auto_review). Bounded
    and best effort: an unanswered question reads as before, as an unreachable remote."""
    d = _delivery(p, cfg)
    tgt = target(p)
    if not tgt:
        return
    tasks = [task]
    if task.get("kind") == "review":
        if d.get("push_queue") is not True or not push_allowed(d):
            return
        if not push.allow_protected(d):
            _default_branch(p, tgt[0], None, True)
        tasks = push._reviewed(p, str(task["id"]))
    for t in tasks:
        if onto := push.pr_elsewhere(p, t, d):
            _absent(p, tgt[0], onto, True)


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
    for note in approval.get("notes") or []:
        text += f"\n{note}"
    for head, ids in (approval.get("inherited") or {}).items():
        text += f"\n{_short(head)} carries commits of #{', #'.join(map(str, ids))}, whose review failed; this review accepted them"
    for head, lines in (approval.get("added") or {}).items():
        text += (f"\nRe-approved after a push conflict; {_short(head)} adds: "
                 + ("; ".join(lines[:20]) if lines else "no new commits"))
    return text


# edges --------------------------------------------------------------------------------------------
def _requeue(db, task: dict, woke: str, extra: dict | None = None) -> None:
    """Run the review again at once and tell its run why (prompts.worker_task shows `woke`)."""
    prev = load_result(task["result"])
    prev.pop(conflictfix.KEY, None)
    db.update_task(task["id"], status="queued", not_before=None, blocked_reason=None,
                   result=dump_result({**prev, "woke": woke[:1500], **(extra or {})}))


def _cancel(db, ids: list[int]) -> None:
    if ids:
        db.x(f"UPDATE push_queue SET status='cancelled', updated=? WHERE status IN ('approved','held') AND id IN "
             f"({','.join('?' * len(ids))})", (time.time(), *ids))


def cancel_orphans(p: Project) -> int:
    """Approved rows of a task that no longer waits for its push are cancelled. The coordinator, the
    web app or `ttp task cancel` moved it (cancelled or queued: expected; anything else is reported).
    Rows already in a batch cannot be withdrawn: the batch reports them."""
    db = p.db
    rows = db.q("SELECT q.id, q.task, t.status FROM push_queue q LEFT JOIN tasks t ON t.id=q.task "
                "WHERE q.status IN ('approved','held') AND (t.status IS NULL OR t.status!='pushing')")
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
                 "WHERE q.status IN ('approved','held') AND t.status='pushing'")
    if not tasks:
        return 0
    with db.tx():
        for t in tasks:
            _cancel(db, [r["id"] for r in db.q("SELECT id FROM push_queue WHERE task=? AND status IN ('approved','held')",
                                                (t["id"],))])
            _requeue(db, t, "push queue turned off: push with ttp push")
            _event(db, t, "task_requeued", f"#{t['id']} {t['title']}: the push queue was turned off; its review "
                                           f"runs again and pushes with ttp push", queued=False)
    return len(tasks)


def retarget(p: Project, name: str) -> int:
    """Approvals for another target (delivery.push_branch changed) send their reviews back."""
    db = p.db
    rows = db.q("SELECT q.id, q.task, q.target FROM push_queue q JOIN tasks t ON t.id=q.task "
                "WHERE q.status IN ('approved','held') AND t.status='pushing' AND q.target!=?", (name,))
    if not rows:
        return 0
    with db.tx():
        for tid in dict.fromkeys(r["task"] for r in rows):
            task = db.task(tid)
            old = next(r["target"] for r in rows if r["task"] == tid)
            _cancel(db, [r["id"] for r in db.q("SELECT id FROM push_queue WHERE task=? AND status IN ('approved','held')",
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
    first) and why; or ([], why not). Only local state is read: no fetch, no checks (an open runner
    fault, envfault, is probed again at most once a minute and holds every batch). Unless the batch
    is full or holds a priority-1 approval or one the last batch requeued untested, it waits for
    delivery.push_min_gap_s since the last one."""
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
        return [], f"backing off until {_at(float(st['backoff_until']), now, p)}"
    if held := envfault.held(p, now):   # a check runner the host lacks: checks would only fail on it
        return [], held
    if not locks.any_free(push.lock_paths(p, remote, branch)) or any(
            not locks.any_free([x]) for x in sorted((p.state / "locks").glob("push:run-*.lock"))):
        return [], "another push holds its turn"
    bad = st.get("bad_tip") or {}
    if bad.get("tip") and _tracking_tip(p, remote, branch) == bad["tip"]:
        # Approvals not yet tried on the failing tip go first: one of them may fix it. With none, the
        # tried ones wait, so the same tip is not checked again and again.
        tried = set(bad.get("tried") or [])
        fresh = [r for r in rows if r["id"] not in tried]
        if not fresh and now < (until := _bad_until(bad)):
            return [], (f"held: the tip {_short(bad['tip'])} fails its checks on its own and every approval was "
                        f"tried on it; next try {_at(until, now, p)} unless the tip or the approvals change")
        rows = fresh + [r for r in rows if r["id"] in tried]
    d = _delivery(p, cfg)
    window = _num(d.get("push_batch_s"), DEFAULT_BATCH_S, float, 0)
    cap = int(_num(d.get("push_batch_max"), DEFAULT_BATCH_MAX, int, 1))
    gap = _num(d.get("push_min_gap_s"), DEFAULT_MIN_GAP_S, float, 0)
    last = db.one("SELECT id, outcome FROM push_batches ORDER BY started DESC LIMIT 1")
    # Approvals the last batch requeued after a blamed entry without checking them: no gap, as it
    # spent its checks on the others (batch._close marks them "untested").
    untested = [r for r in rows if last and r["batch"] == last["id"] and _detail(r).get("untested")]
    if len(rows) >= cap:
        why = f"{len(rows)} approvals"
    elif last and last["outcome"] == "rebooted":
        why = f"a rerun of {last['id']}, cut short by a reboot"
    elif any(r["priority"] == 1 for r in rows):
        why = "a priority-1 review"
    elif untested:
        why = f"{len(untested)} approval{'s' if len(untested) != 1 else ''} {last['id']} requeued untested"
    elif (left := _gap_left(db, now, gap)) > 0:
        return [], f"{left:.0f} s left of the {gap:.0f} s gap since the last batch ended"
    elif now - min(r["created"] for r in rows) >= window:
        why = f"the oldest approval waited {window:.0f} s"
    elif not db.one("SELECT id FROM tasks WHERE kind='review' AND status='running' LIMIT 1") and not any(
            t["kind"] == "review" for t in db.ready_tasks()):
        why = "no review is running or ready"
    else:
        return [], "waiting for more approvals"
    taken, held = _one_solo(p, rows, cap, _solo_paths(d), _tracking_tip(p, remote, branch))
    if held:
        why += (f"; {len(held)} more approval{'s' if len(held) != 1 else ''} touching delivery.push_solo_paths "
                f"wait{'s' if len(held) == 1 else ''} for a later batch (one per batch)")
    return taken, why


def _solo_paths(d: dict) -> list[str]:
    v = d.get("push_solo_paths", SOLO_PATHS)
    v = [v] if isinstance(v, str) else v if isinstance(v, (list, tuple)) else []
    return [str(x) for x in v if str(x).strip()]


def _one_solo(p: Project, rows: list[dict], cap: int, paths: list[str], tip: str) -> tuple[list[dict], list[dict]]:
    """Up to `cap` of `rows` in order, at most one of them a change touching `paths` since it left
    `tip`: (taken, the touching ones left for a later batch). A row git cannot tell about counts as
    touching (one per batch is the safe side)."""
    if not paths or len(rows) < 2:
        return rows[:cap], []
    taken, held, solo = [], [], False
    for r in rows:
        if len(taken) >= cap:
            break
        if _touches_paths(p, tip, f"{REF_PREFIX}{r['id']}", paths):
            if solo:
                held.append(r)
                continue
            solo = True
        taken.append(r)
    return taken, held


def _touches_paths(p: Project, tip: str, ref: str, paths: list[str]) -> bool:
    if not tip:
        return True
    try:
        r = _git(p, "diff", "--name-only", f"{tip}...{ref}", "--", *paths, timeout=INHERIT_GIT_S)
    except (OSError, subprocess.SubprocessError):
        return True
    return r.returncode != 0 or bool(r.stdout.strip())


def _bad_until(bad: dict) -> float:
    """When approvals already tried on the failing tip of `bad` (state "bad_tip") may run again."""
    wait = BAD_TIP_RETRY_S if int(bad.get("fails") or 0) >= TIP_FAILS else HOLD_S
    return float(bad.get("at") or 0) + wait


def _known_bad(db, rows: list[dict]) -> dict | None:
    """The marker's "bad_tip" for a batch of `rows`: the tip that failed its checks on its own in
    TIP_FAILS batches in a row, with how many leading rows were not tried on it ("fresh"). The batch
    then never checks that tip alone again (batch.Batch._judge)."""
    bad = _state(db).get("bad_tip") or {}
    if not bad.get("tip") or int(bad.get("fails") or 0) < TIP_FAILS:
        return None
    tried = set(bad.get("tried") or [])
    fresh = next((n for n, r in enumerate(rows) if r["id"] in tried), len(rows))
    return {"tip": bad["tip"], "fresh": fresh, "cmd": bad.get("cmd")}


def bad_tip_ids(db) -> list[str]:
    """The failing test ids the known-bad tip's own checks showed (sharedchecks), [] when none is known."""
    bad = _state(db).get("bad_tip") or {}
    if not bad.get("tip") or int(bad.get("fails") or 0) < TIP_FAILS or not isinstance(bad.get("ids"), list):
        return []
    return [str(i) for i in bad["ids"]]


def touches(p: Project, old: str, new: str, files: list[str]) -> bool:
    """A commit in old..new changes one of `files`; True when git cannot tell (a rerun is the safe side)."""
    try:
        r = _git(p, "log", "--format=%H", f"{old}..{new}", "--", *files, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True
    return r.returncode != 0 or bool(r.stdout.strip())


def batch_argv(marker: Path) -> list[str]:
    """The batch process (T1's `ttp push --batch`); tests put a stub here."""
    return [sys.executable, "-m", "ttp", "push", "--batch", str(marker)]


def _child_env(p: Project, output: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("TTP_RUN_DIR", "TTP_TASK", "TTP_TASK_ID", "TTP_RUN_ID", "TTP_PIDNS", "TTP_CGROUP")}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), TTP_PROJECT=str(p.base),
               TTP_RUN_DIR=output, TTP_CHECK_OUTPUT_DIR=output)
    return checkdirs.environment(Path(output), env)


def _spawn(p: Project, bid: str, marker: Path, lock) -> subprocess.Popen:
    """Start the batch process on `marker`, holding `lock` from its first instant (inherited, as
    push.detach does), in a session of its own so a daemon restart leaves it running. It starts once
    its stdin closes: the go, given after the marker is complete. It runs niced like the workers
    (runner.nice), checks and after_push commands included."""
    output = checkdirs.restore(p.state, push._read(marker).get("check_run_dir"))
    guard = checkdirs.take(output)
    if guard is None:
        raise OSError("check scratch is busy")
    try:
        env = _child_env(p, str(output))
        env["TTP_BATCH_LOCK_FD"] = str(lock.fileno())
        env["TTP_CHECKS_TMP_FD"] = str(guard.fileno())
        with open(marker.with_suffix(".log"), "ab") as out:
            child = subprocess.Popen(batch_argv(marker), cwd=str(p.root), env=env, stdin=subprocess.PIPE,
                                     stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                     pass_fds=(lock.fileno(), guard.fileno()))
        renice(child.pid, nice_level(p.config().get("runner"))[0])
        _children[bid] = child
        return child
    except BaseException:
        checkdirs.finish(output)
        raise
    finally:
        guard.close()
        checkdirs.cleanup(output, whole=True)


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
        if bad := _known_bad(db, rows):
            m["bad_tip"] = bad
        m["check_run_dir"] = str(checkdirs.new(p.state, marker))
        m["check_report_dir"] = str(checkdirs.reports(Path(m["check_run_dir"])))
        write_json(marker, m)
        ids = [r["id"] for r in rows]
        with db.tx():
            took = db.conn.execute(f"UPDATE push_queue SET status='batched', batch=?, detail=NULL, updated=? "
                                   f"WHERE status='approved' "
                                   f"AND id IN ({','.join('?' * len(ids))})", (bid, now, *ids)).rowcount
            if took != len(ids):
                raise _Changed()
            db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)", (bid, str(marker), name, now))
        # From here on finalize owns the batch: a process that never starts is a batch that died.
        child = _spawn(p, bid, marker, lock)
        m["pid"] = child.pid
        write_json(marker, m)
        child.stdin.close()      # the go
        prev = db.one("SELECT * FROM push_batches WHERE outcome='rebooted' AND id!=? ORDER BY started DESC LIMIT 1",
                      (bid,))
        if prev and _read(Path(prev["marker"])).get("rerun_by") is None:
            _close_marker(prev, {"rerun_by": bid})
    except _Changed:
        marker.unlink(missing_ok=True)
        checkdirs.finish(Path(m["check_run_dir"]))
        return None
    finally:
        lock.close()             # the child holds the run lock from here on
    return bid


class _Changed(Exception):
    """Rows left `approved` between due() and start(): nothing starts."""


def _forget_moved_tip(p: Project, remote: str, branch: str) -> None:
    """Drop the failing tip once the branch has moved past it (as last fetched): that clears its alert."""
    db = p.db
    st = _state(db)
    bad = st.get("bad_tip") or {}
    if bad.get("tip") and (now := _tracking_tip(p, remote, branch)) and now != bad["tip"]:
        st.pop("bad_tip", None)
        db.set_kv(KV, st)


def schedule(p: Project, cfg: dict | None = None, log: Callable[[str], None] = lambda m: None) -> str | None:
    """The tick's start step: send back approvals for another target, then start a batch if one is
    due. Cheap while nothing is approved: no locks, and no git unless a failing tip is remembered."""
    db = p.db
    if not enabled(p, cfg):
        return None
    if (_state(db).get("bad_tip") or {}).get("tip") and (tgt := target(p)):
        _forget_moved_tip(p, *tgt)
    if not db.one("SELECT id FROM push_queue WHERE status='approved' LIMIT 1"):
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


def _reboot_in(p: Project, started: float, now: float) -> float | None:
    """The first host boot after `started` and by `now`: one the daemon recorded (DB.boots), or this
    boot's own time while the daemon has not recorded it yet. None when the host did not reboot."""
    times = [float(x.get("boot_time") or x["ts"]) for x in p.db.boots(started)]
    if (this := runner.boot_time()) is not None:
        times.append(this)
    return min((t for t in times if started < t <= now), default=None)


def _close_marker(b: dict, extra: dict | None = None) -> None:
    """Rewrite the batch's marker as finished with the DB row's outcome and end (atomic, never inside a
    transaction), so a file never stays `running` once its row is final. A live batch is left alone."""
    marker = Path(b["marker"])
    m = _read(marker)
    if not m or m.get("kind") != "batch" or not b.get("finalized"):
        return
    if m.get("status") == "finished" and m.get("outcome") and not extra:
        return
    if locks.any_free([Path(m["lock"])]) is False:
        return
    m.update(extra or {})
    if m.get("status") != "finished" or not m.get("outcome"):
        m.update(status="finished", phase="finished", outcome=b["outcome"], ended=b["ended"])
    write_json(marker, m)


def close_strays(p: Project) -> list[str]:
    """At daemon start: close the marker of every batch whose DB row is already final but whose file
    still says `running`. Rows still live are left alone."""
    done = []
    rows = p.db.q("SELECT * FROM push_batches WHERE finalized IS NOT NULL ORDER BY started")
    for i, b in enumerate(rows):
        m = _read(Path(b["marker"]))
        if m and m.get("kind") == "batch" and m.get("status") == "running" and not m.get("outcome"):
            nxt = next((r["id"] for r in rows[i + 1:]), None) if b["outcome"] == "rebooted" else None
            _close_marker(b, {"rerun_by": nxt} if nxt else None)
            done.append(b["id"])
    return done


def _died(p: Project, b: dict, m: dict, alert: Callable, why: str, now: float, queued: bool = False,
          rebooted: float | None = None) -> None:
    was = p.db.one("SELECT finalized FROM push_batches WHERE id=?", (b["id"],))
    _died_db(p, b, m, alert, why, now, queued, rebooted)
    if was and was["finalized"] is None:
        row = p.db.one("SELECT * FROM push_batches WHERE id=?", (b["id"],))
        if row and row["finalized"]:
            _close_marker(row)


def _died_db(p: Project, b: dict, m: dict, alert: Callable, why: str, now: float, queued: bool = False,
             rebooted: float | None = None) -> None:
    """A batch that ended without a usable outcome: its rows go back to approved, a try counted, and
    the queue backs off. DYING_AFTER of them in a row raise push_queue_dying. `queued`: the event
    needs the coordinator (something is left that the queue cannot recover). `rebooted`: the boot
    that cut it short; then no try is counted, nothing backs off and the batch runs again at once
    (due), as the batch itself never failed."""
    db = p.db
    outcome = "error" if m.get("outcome") == "error" else "rebooted" if rebooted else "died"
    with db.tx():
        if not db.conn.execute("UPDATE push_batches SET outcome=?, ended=?, finalized=?, after_push='skipped', "
                               "after_finalized=? WHERE id=? AND finalized IS NULL",
                               (outcome, now, now, now, b["id"])).rowcount:
            return
        rows = db.q("SELECT id, tries FROM push_queue WHERE batch=? AND status='batched'", (b["id"],))
        if rebooted:
            db.x("UPDATE push_queue SET status='approved', updated=? WHERE batch=? AND status='batched'",
                 (now, b["id"]))
            st = _state(db)
            st.pop("backoff_until", None)
            db.set_kv(KV, st)
            _event(db, None, "push_batch_died",
                   f"push batch {b['id']} was cut short by the {_at(rebooted, now, p)} reboot; {len(rows)} approval"
                   f"{'s' if len(rows) != 1 else ''} back in the queue, run again at once", queued=False)
            push._forget_lock(Path(b["marker"]))
            return
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
    mirrors = [f"{x['remote']}/{x['branch']}" for x in push.mirror_list(_delivery(p).get("mirror_remotes"))]
    if also or mirrors:
        m["fast_forward"] = [f"not ff {x}: the batch stopped after its push, before this step (a reboot?); "
                             f"not run" for x in also] + \
                            [f"not mirrored {x}: the batch stopped after its push, before this step (a reboot?); "
                             f"not run" for x in mirrors]
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
    # Counted from its events: each hand-off replaces the review's result.
    trims = db.one("SELECT COUNT(*) n FROM events WHERE task=? AND kind='task_requeued' AND text LIKE ?",
                   (tid, f"%{TRIM_EVENT}%"))["n"]
    trim = None
    if "check_failed" in states and trims < conflictfix.TRIM_MAX:
        r = next(r for r in current if r["status"] == "check_failed")
        trim = conflictfix.cap_failure(str(_detail(r).get("tail") or ""), _delivery(p).get("push_cap_tests"))
    if trim:
        # Only prompt size caps failed: the change fit alone, not with what landed before it. A light
        # run trims it to fit and approves again, instead of a failed review (and a shared-check incident).
        d = _detail(r)
        woke = (f"the push checks `{d.get('cmd') or 'the push checks'}` failed in batch {b['id']} only on prompt "
                f"size caps, on {r['branch'] or '?'} at {_short(r['head'])} merged onto {target}: "
                f"{conflictfix.over_text(trim)}")
        cf = conflictfix.mark(str(r["head"] or ""), r["branch"] or "", target, str(m.get("tip") or ""), [], True,
                              trims + 1, trim=trim)
        _requeue(db, task, woke, {conflictfix.KEY: cf, conflictfix.TRIMS: trims + 1})
        _event(db, task, "task_requeued", f"#{tid} {task['title']}: {woke}; {TRIM_EVENT}", queued=False)
    elif "check_failed" in states:
        r = next(r for r in current if r["status"] == "check_failed")
        d = _detail(r)
        cmd, tail = str(d.get("cmd") or "the push checks"), _tail_of(m, d)
        text = (f"the push checks failed on {r['branch'] or '?'} at {_short(r['head'])} in batch {b['id']}: "
                f"`{cmd}`\n{tail}")
        # The review found no code fault: only the checks failed (reviewcap's area cap and the
        # coordinator's effort rules read review_failure and the failing ids).
        ids, complete = sharedchecks.failing_ids(str(d.get("tail") or ""))
        db.update_task(tid, status="failed", blocked_reason=None, result=dump_result(
            {**prev, "status": "failed", "summary": f"{text}\n(review: {summary})"[:1500],
             "review_failure": "check_only", sharedchecks.FAILING_KEY: ids, sharedchecks.COMPLETE_KEY: complete,
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
            woke += f"; its checks failed after the batch merged both sides' changes: {d['cmd']}"
            if tail:
                woke += f" ({tail[-300:]})"
        done = [x for x in current if x["status"] in ("pushed", "landed")]
        if done:
            woke += "; already landed: " + ", ".join(f"{x['branch'] or '?'} at {_short(x['head'])} ({x['status']} as "
                                                     f"{_short(x['pushed_sha'])})" for x in done)
        # Conflict fixes cost no review attempt (conflictfix.py): their own count is the approvals the
        # queue sent back, this one included.
        sent = db.one("SELECT COUNT(DISTINCT created) n FROM push_queue WHERE task=? AND status='conflict'",
                      (tid,))["n"]
        if sent > int(task["max_attempts"] or 3):
            text = f"{woke}. It keeps conflicting after {sent - 1} conflict fixes"
            rebase = auto_rebase(p, task, r, target, woke)
            if rebase:
                text += f"; the daemon queued #{rebase} to rebase it once more and re-approve it"
            db.update_task(tid, status="failed", blocked_reason=None, result=dump_result(
                {**prev, "status": "failed", "summary": text[:1500], **({"auto_rebase": rebase} if rebase else {})}))
            # With a rebase queued, nothing is the coordinator's to decide yet: its own failure is.
            _event(db, task, "task_failed", f"#{tid} {task['title']} → failed: {text}", queued=not rebase)
            if not rebase:
                _reply(db, task, "failed", text)
        else:
            raw = [str(f) for f in (d.get("files") or [])]
            textual = bool(raw) and not raw[0].startswith("(the rebase") and not d.get("cmd")
            cf = conflictfix.mark(str(r["head"] or ""), r["branch"] or "", target,
                                  str(d.get("onto") or m.get("tip") or ""), raw, textual, sent)
            _requeue(db, task, woke, {conflictfix.KEY: cf})
            _event(db, task, "task_requeued", f"#{tid} {task['title']}: {woke}; its review runs again to settle it"
                   + ("" if cf.get("escalate") else " as a light conflict fix"), queued=False)
    elif states <= {"pushed", "landed"}:
        done = [x for x in rows if x["status"] in ("pushed", "landed")]
        last = max(done, key=lambda x: x["id"])
        new = [x for x in current if x["status"] == "pushed"]
        tail = _landing({**last, "status": "pushed" if new else "landed"}, last["target"] or target,
                        _reach(db, last["batch"], b, m))
        ff = [str(x) for x in m.get("fast_forward") or []] if new else []
        if ff:   # delivery.fast_forward_also and mirror_remotes, as read back from the remotes
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
        _reply(db, task, "done", _done_text(prev, result["summary"], tail))
        settle_reviewed(db, now, only=_covers(db, task, rows))
    # else: some of its rows are still approved or batched; it keeps waiting.


def rebase_head(task: dict) -> str:
    """The conflicting head a push-conflict rebase review (auto_rebase) rebases, from its
    `push_rebase:<head>` label, or ""."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return ""
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith(REBASE_LABEL) and _HEX40.fullmatch(lb[len(REBASE_LABEL):]):
            return lb[len(REBASE_LABEL):]
    return ""


def auto_rebase(p: Project, review: dict, row: dict, target: str, woke: str) -> int | None:
    """When a review's approved head keeps conflicting after its attempts, one review task that
    continues it to rebase that head onto the push branch's tip, resolve keeping both sides' intents
    and approve the new head again: its id, inside _settle's transaction. None, so the review fails
    to the coordinator as before, when the review is itself such a rebase or continues one, the head
    already had one, a task already continues the review, or the rolling review-task cap
    (coordinator.max_review_tasks_per_day) allows no new review now."""
    db, head = p.db, str(row["head"] or "")
    if not _HEX40.fullmatch(head):
        return None
    seen, t = set(), review
    while t and t["id"] not in seen and len(seen) < 50:   # the review and the tasks it continues
        seen.add(t["id"])
        if f'"{REBASE_LABEL}' in (t.get("labels") or ""):
            return None
        c = continues_id(t)
        t = db.task(c) if c is not None else None
    if db.one("SELECT id FROM tasks WHERE labels LIKE ? OR labels LIKE ?",
              (f'%"{REBASE_LABEL}{head}"%', f'%"continues:{review["id"]}"%')):
        return None
    cfg = p.config()
    if coord.next_task_slot(db, coord.task_cap(cfg, True), review=True) is not None:
        return None
    try:
        labels = json.loads(review.get("labels") or "[]")
    except (ValueError, TypeError):
        labels = []
    keep = [lb for lb in labels if isinstance(lb, str) and lb.startswith("auto_review:")] \
        if isinstance(labels, list) else []
    branch = row["branch"] or ""
    tier = "deep" if review["tier"] == "deep" else "standard"
    spec = "\n".join([
        f"Review #{review['id']} ({review['title'][:150]}) passed, but the push queue could not land its approved "
        f"head after {review['attempts']} attempts: {woke}.",
        f"Branch: {branch or '(none)'} at {head}. Push branch: {target}.",
        "Woken because `push conflict`: fetch, rebase that head onto the push branch's current tip in the change's "
        "worktree and resolve every conflict keeping both sides' intents (the one edit you may make).",
        "Then run the project's push checks (delivery.push_checks) on the rebased head with `ttp checks` and fix "
        "only what the resolution broke. Once they pass, commit and hand off `done` with "
        f'"push": [{{"branch": "{branch}", "head": "<the rebased head>"}}] to approve it into the push queue again.',
        "If the conflict needs a decision about either change's intent, hand off `failed` saying what it is.",
        "Return the rebased head and the check results."])
    rid = db.add_task(f"Rebase {branch or _short(head)} onto {target} after push conflicts (review #{review['id']})",
                      spec, kind="review", tier=tier, priority=review["priority"], origin="daemon",
                      reply_chat=review["reply_chat"],
                      budget_usd=float(((cfg.get("budget") or {}).get("task_default_usd") or {}).get(tier, 8.0)),
                      depends_on=[d for d in dependency_ids(review) if isinstance(d, int)],
                      labels=[*keep, f"continues:{review['id']}", f"{REBASE_LABEL}{head}"], **pin_of(review))
    return rid


# reviewed code tasks ------------------------------------------------------------------------------
def _covers(db, review: dict, rows: list[dict], code: list[dict] | BranchIndex | None = None,
            follow: bool = True) -> set[int]:
    """The tasks whose work a review's push ships: its parent and dependencies, the task its title or
    label names, the code tasks whose branch it pushed or (naming no subject) its spec names, and,
    with `follow`, the tasks each of those continues (a fix carries the change it fixes)."""
    ids = {d for d in dependency_ids(review) if isinstance(d, int)}
    ids |= {i for i in (review.get("parent"), review_subject(review)) if isinstance(i, int)}
    branches = {r["branch"] for r in rows if r["branch"]}
    if code is None:
        code = db.q("SELECT id, branch, labels FROM tasks WHERE kind='code' AND branch IS NOT NULL AND branch!=''")
    index = code if isinstance(code, BranchIndex) else BranchIndex(code)
    ids |= {i for b in branches for i in index.by_branch.get(b, ())} | reviewed_ids(review, index)
    if not follow:
        return ids
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
    code = BranchIndex(db.q("SELECT id, branch, labels FROM tasks WHERE kind='code' AND branch IS NOT NULL "
                            "AND branch!=''"))
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
            _reply(db, t, "done", _done_text(prev, result["summary"], tail))
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
        shipped: list[int] = []
        for r in rows:
            res = results.get(r["id"]) or {}
            status = res.get("status") if res.get("status") in ROW_RESULTS else (
                "refused" if outcome == "refused" else "requeued")
            detail = res.get("detail") if isinstance(res.get("detail"), dict) else None
            if outcome == "refused" and not detail:
                detail = {"why": str(m.get("message") or "")[:2000] or None}
            text = json.dumps(detail)[:8000] if detail else None
            if status in ("pushed", "landed"):
                shipped.append(r["task"])
                own = res.get("sha") if isinstance(res.get("sha"), str) and _HEX40.fullmatch(res["sha"]) else None
                db.x("UPDATE push_queue SET status=?, pushed_sha=?, landed_sha=?, version=?, detail=?, updated=? "
                     "WHERE id=?", (status, sha or tip, own or sha or tip, version, text, now, r["id"]))
            elif status in ("conflict", "check_failed", "refused"):
                db.x("UPDATE push_queue SET status=?, detail=?, updated=? WHERE id=?", (status, text, now, r["id"]))
            elif status == "scope_held":   # its push needs a token scope the credential lacks (scopehold)
                db.x("UPDATE push_queue SET status='held', detail=?, updated=? WHERE id=?", (text, now, r["id"]))
            else:   # requeued, or the batch did not get to it (busy, moved, rejected, tip_failed)
                db.x("UPDATE push_queue SET status='approved', tries=tries+?, detail=?, updated=? WHERE id=?",
                     (1 if outcome == "moved" else 0, text, now, r["id"]))
        st = _state(db)
        st["deaths"] = 0
        back = [r["id"] for r in db.q("SELECT id FROM push_queue WHERE batch=? AND status='approved'", (b["id"],))]
        st.pop("hold", None)   # the time-boxed hold of older releases; bad_tip replaces it
        bad = st.get("bad_tip") or {}
        d = m.get("tip_check") if isinstance(m.get("tip_check"), dict) else {}
        first = next((x.get("detail") for x in results.values() if isinstance(x.get("detail"), dict)
                      and x["detail"].get("cmd")), {}) or {}
        cmd = d.get("cmd") or first.get("cmd") or "the push checks"
        if outcome == "tip_failed" and tip:
            same = bad.get("tip") == tip
            fails = int(bad.get("fails") or 0) + 1 if same else 1
            tried = set(bad.get("tried") or []) if same else set()
            # Only the approvals the batch showed do not fix the tip are tried; the rest go first next
            # time. A marker from a release before "tip_tried" counts every approval it carried.
            shown = m.get("tip_tried") if isinstance(m.get("tip_tried"), list) else [r["id"] for r in rows]
            tried |= {r["id"] for r in rows if r["id"] in shown}
            st["bad_tip"] = {"tip": tip, "fails": fails, "tried": sorted(tried), "at": now,
                             "cmd": cmd, "alerted": bool(same and bad.get("alerted")) or fails >= TIP_FAILS,
                             "ids": sharedchecks.failing_ids(str(d.get("tail") or first.get("tail") or ""))[0]}
            if fails >= TIP_FAILS and not (same and bad.get("alerted")):
                later.append((BAD_TIP_ALERT, f"The push branch {b['target']} fails its own checks at {_short(tip)} "
                                             f"(`{cmd}`) in {fails} push batches in a row. The queue no longer checks "
                                             f"that tip alone: it tries approvals not yet tried on it first, and "
                                             f"pushes them if the tip with them passes. This clears once the tip "
                                             f"changes or passes.", "high"))
        elif bad.get("tip") and (outcome == "pushed" or (outcome in ("landed", "nothing") and m.get("tip_passed"))
                                 or (tip and tip != bad["tip"])):
            # The tip passed (with what was pushed, or alone), or moved: its alert clears (alerts.holds).
            # A batch that carried nothing, or that the version guard stopped, never showed it passing.
            st.pop("bad_tip", None)
        if outcome in ("pushed", "landed", "nothing"):
            st.pop("backoff_until", None)
        if outcome == "tip_failed" and tip and tip not in (st.get("tips_told") or []):
            st["tips_told"] = ((st.get("tips_told") or []) + [tip])[-TIPS_TOLD:]
            _event(db, None, "push_tip_failed", f"the push branch tip {_short(tip)} of {b['target']} fails its checks: "
                                                f"`{cmd}`\n{_tail_of(m, d or first)}", queued=True, severity="high")
        _flaky_told(db, st, b, m, now)
        if warn := push.ff_warnings(m.get("fast_forward")):
            _event(db, None, "push_not_ff", f"push batch {b['id']} pushed {_short(sha)} to {b['target']}, but "
                                            f"delivery.fast_forward_also or delivery.mirror_remotes did not move "
                                            f"every branch: "
                                            + "; ".join(warn), queued=True)
        if outcome == "rejected":
            st["backoff_until"] = now + BACKOFF_MAX_S
            later.append(("push_rejected", f"The push queue's push to {b['target']} was rejected: "
                                           f"{str(m.get('message') or _tail_of(m, None, 5))[:1500]}. "
                                           f"The approvals stay queued and are retried in {BACKOFF_MAX_S // 60} min.", "high"))
        db.set_kv(KV, st)
        later += _hold_scoped(p, b, m, now)
        for tid in dict.fromkeys(r["task"] for r in rows):
            _settle(p, tid, b, m, now)
        # In the transaction that finalizes the batch: a crash before it commits records them again.
        _resolve_upstream(p, shipped, version)
        if outcome in ("moved", "busy", "env_fault", "rejected", "nothing", "landed", "pushed", "tip_failed") and back:
            _event(db, None, "push_requeued", f"push batch {b['id']} ended {outcome}: {len(back)} approval"
                                              f"{'s' if len(back) != 1 else ''} back in the queue", queued=False)
    for key, text, sev in later:
        alert(key, text, severity=sev)


def _hold_scoped(p: Project, b: dict, m: dict, now: float) -> list[tuple]:
    """Rows the batch held because their push needs a token scope the credential lacks (batch
    _hold_scoped): record them under their scope (scopehold.KV) and tell their reviews. Returns the
    alert for each scope not held already: one per condition, until the held changes push."""
    db = p.db
    held = db.q("SELECT q.*, t.title FROM push_queue q LEFT JOIN tasks t ON t.id=q.task WHERE q.batch=? "
                "AND q.status='held' ORDER BY q.id", (b["id"],))
    if not held:
        return []
    rec = db.kv(scopehold.KV) or {}
    d = _delivery(p)
    others = db.one("SELECT COUNT(*) n FROM push_queue WHERE batch=? AND status!='held'", (b["id"],))["n"]
    outcome, sha, msg = str(m.get("outcome")), m.get("pushed_sha"), str(m.get("message") or "")
    if not others:
        rest = "Nothing else was in the batch."
    elif outcome == "pushed":
        rest = f"The rest of the batch was pushed as {_short(sha)}."
    elif outcome == "landed":
        rest = "The rest of the batch was already on the branch."
    else:
        rest = (f"The rest of the batch was not pushed: {OUTCOME_WORDS.get(outcome, outcome)}"
                + (f" ({msg[:300]})" if msg else "") + ".")
    later = []
    for scope in dict.fromkeys(_detail(r).get("scope") or "?" for r in held):
        rows = [r for r in held if (_detail(r).get("scope") or "?") == scope]
        files = list(dict.fromkeys(f for r in rows for f in _detail(r).get("files") or []))
        check = str(_detail(rows[0]).get("check") or scopehold.check_cmd(d, scope))
        prev = rec.get(scope)
        rec[scope] = {"rows": sorted(set((prev or {}).get("rows") or []) | {r["id"] for r in rows}),
                      "since": (prev or {}).get("since") or now, "probe_at": now,
                      "holds": int((prev or {}).get("holds") or 0) + (1 if not prev or prev.get("released") else 0)}
        need = f"needs the token scope `{scope}` for {', '.join(files[:5]) or 'its files'}"
        for r in rows:
            task = db.task(r["task"])
            if task and task["status"] == "pushing":
                db.update_task(r["task"], blocked_reason=f"push held: {need}; back in the queue once `{check}` "
                                                         f"passes"[:500])
            _event(db, task, "push_held", f"#{r['task']} {r['title'] or ''}: push held in batch {b['id']}, "
                                          f"{r['branch'] or '?'} at {_short(r['head'])} {need}", queued=False)
        if prev:
            continue   # the condition's alert is out already
        n = len(rows)
        tasks = "; ".join(f"#{r['task']} {r['title'] or ''} ({r['branch'] or '?'} at {_short(r['head'])})" for r in rows)
        every = scopehold.probe_s(d) / 60
        later.append((f"{scopehold.ALERT}:{scope}",
                      f"The push to {b['target']} was refused: the token it pushes with lacks the `{scope}` scope, "
                      f"needed to change {', '.join(files[:10]) or 'the files it names'}. Held: {tasks}. {rest} "
                      f"A user with the account must grant the scope (on GitHub: {scopehold.grant_hint(scope)}); "
                      f"the held change{'s go' if n != 1 else ' goes'} back into the queue by itself once `{check}` "
                      f"passes (checked every {every:.0f} min).", "high"))
    db.set_kv(scopehold.KV, rec)
    return later


def tend_scope_holds(p: Project, cfg: dict | None = None, now: float | None = None) -> list[int]:
    """Model-free: check each token scope held changes wait for (scopehold.probe, at most every
    delivery.push_scope_probe_s) and put its rows back in the queue once it passes. A scope is over
    (its alert clears, alerts.holds) once none of its rows waits: they pushed, failed or were withdrawn.
    Returns the rows put back."""
    db = p.db
    rec = db.kv(scopehold.KV) or {}
    held = db.q("SELECT * FROM push_queue WHERE status='held' ORDER BY id")
    if not rec and not held:
        return []
    now = time.time() if now is None else now
    d = _delivery(p, cfg)
    before = json.dumps(rec, sort_keys=True)
    by_scope: dict[str, list[dict]] = {}
    for r in held:
        by_scope.setdefault(_detail(r).get("scope") or "?", []).append(r)
    live = {r["id"] for r in db.q("SELECT id FROM push_queue WHERE status IN ('held','approved','batched')")}
    for scope in list(rec):
        rec[scope]["rows"] = [i for i in rec[scope].get("rows") or [] if i in live]
        if not rec[scope]["rows"] and scope not in by_scope:
            del rec[scope]
    back: list[int] = []
    for scope, rows in by_scope.items():
        e = rec.setdefault(scope, {"rows": [r["id"] for r in rows], "since": now, "probe_at": now, "holds": 1})
        if now - float(e.get("probe_at") or 0) < scopehold.probe_s(d, int(e.get("holds") or 1)):
            continue
        ok, out = scopehold.probe(p.root, d, scope)
        e["probe_at"], e["probe"] = now, out
        if not ok:
            continue
        e["released"] = now
        check = scopehold.check_cmd(d, scope)
        with db.tx():
            for r in rows:
                db.x("UPDATE push_queue SET status='approved', detail=?, updated=? WHERE id=? AND status='held'",
                     (json.dumps({**_detail(r), "released": now}), now, r["id"]))
            for tid in dict.fromkeys(r["task"] for r in rows):
                task = db.task(tid)
                if task and task["status"] == "pushing":
                    db.update_task(tid, blocked_reason=None)
                _event(db, task, "push_released", f"#{tid}: the token scope `{scope}` check passed (`{check}`); "
                                                  f"its held push is back in the queue", queued=False)
        back += [r["id"] for r in rows]
    if json.dumps(rec, sort_keys=True) != before:
        db.set_kv(scopehold.KV, rec)
    return back


def resolves_of(p: Project, review_id: int) -> list[tuple[str, dict]]:
    """The upstream note fingerprints (upstream.fingerprint) the change review `review_id` approved
    fixes, each with the task that names it: the `resolves` lists in the hand-offs of the review, of
    the code tasks it covers (push._reviewed), of the tasks whose commits it accepted (`inherited` in
    its push entries) and of the tasks they continue, plus the open known notes their specs name as
    `[note <fp>]`. A note several hand-offs list goes to the earliest done code task that lists it:
    the one the note was given to, not a later task that only continued or unblocked it."""
    review = p.db.task(review_id)
    tasks = [review] if review else []
    seen: set[int] = set()
    entries = load_result(review.get("result")).get("push") if review else None
    inherited = [p.db.task(int(str(i).lstrip("#"))) for e in (entries if isinstance(entries, list) else [])
                 if isinstance(e, dict) and isinstance(e.get("inherited"), list)
                 for i in e["inherited"] if str(i).lstrip("#").isdigit()]
    for t in push._reviewed(p, str(review_id)) + [t for t in inherited if t]:
        while t and t["id"] not in seen:
            seen.add(t["id"])
            tasks.append(t)
            c = continues_id(t)
            t = p.db.task(c) if c is not None else None
    out: dict[str, dict] = {}
    for t in sorted(tasks, key=lambda t: (t.get("kind") == "review", t.get("status") != "done", t["id"])):
        got = load_result(t.get("result")).get("resolves")
        for fp in (got if isinstance(got, list) else [got] if isinstance(got, str) else []):
            fp = str(fp).strip().lower()
            if re.fullmatch(r"[0-9a-f]{16}", fp) and len(out) < RESOLVES_MAX:
                out.setdefault(fp, t)
    for t in tasks:   # a split or fix task may drop `resolves`: the specs still name the notes
        for fp in re.findall(r"\[note ([0-9a-fA-F]{16})\]", str(t.get("spec") or "")):
            fp = fp.lower()
            if len(out) < RESOLVES_MAX and fp not in out and upstream._filed(fp) and upstream.state(fp) == "open":
                out[fp] = t
    return list(out.items())


def _resolution_text(t: dict) -> str:
    """What a note's fixed-in record says changed: the task that names it, with its own hand-off's
    summary (its title when it has none), never another task's."""
    summary = " ".join(str(load_result(t.get("result")).get("summary") or "").split())
    return f"#{t['id']} {summary or t.get('title') or ''}"[:300]


def _resolve_upstream(p: Project, review_ids: list[int], version: str | None) -> None:
    """Record each upstream note the pushed approvals fix (resolves_of) as fixed in the release the
    batch pushed (upstream.resolve). The same resolution twice is left alone, so a batch applied again
    after a crash adds none; a failure to record is an event, never a failed batch."""
    if not version or not review_ids:
        return
    done, failed = [], []
    for rid in dict.fromkeys(review_ids):
        for fp, t in resolves_of(p, rid):
            try:
                if upstream.wontfixed(fp):
                    continue
                had = upstream.resolution(fp)
                if had and had.get("fixed_in") == str(version).lstrip("v"):
                    continue
                upstream.resolve(fp, version, _resolution_text(t), p.name)
                done.append(fp)
            except (OSError, ValueError) as e:
                failed.append(f"{fp} ({e})")
    if done:
        _event(p.db, None, "upstream_resolved", f"push of {version} resolved upstream note"
                                                f"{'s' if len(done) != 1 else ''} {', '.join(done)}",
               queued=False, severity="low")
    if failed:
        _event(p.db, None, "upstream_resolve_failed", f"push of {version} could not record the resolution of "
                                                      f"upstream notes {'; '.join(failed)[:2000]}. Record it with "
                                                      f"`ttp upstream --resolve <fp> --fixed-in {version}`",
               queued=True)


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
    if status == "failed" and _deploy_alerted(p, m, ap):
        return   # its deploy verdict already sent the one alert, naming each project behind
    if status in ("failed", "timeout", "killed"):
        alert("after_push_failed", f"after_push of {what} (batch {b['id']}) {how}. Log: {m.get('log') or '?'}",
              severity="high")


def _deploy_alerted(p: Project, m: dict, ap: dict) -> bool:
    """A failed after_push whose deploy verdict (`ttp deployed --after-push`, deploy.record) already alerted
    for this commit: one deploy outcome sends one alert, so the push queue sends none of its own."""
    rec = p.db.kv(DEPLOY_ALERTED) or {}
    a, b = str(rec.get("commit") or ""), str(m.get("pushed_sha") or "")
    return (len(min(a, b, key=len)) >= 7 and (a.startswith(b) or b.startswith(a))
            and float(rec.get("at") or 0) >= float(ap.get("started") or 0))


def _after_push_set(p: Project, cfg: dict | None) -> bool:
    v = _delivery(p, cfg).get("after_push")
    if v is None or str(v).strip().lower() in ("", "none"):
        return False
    try:
        return bool(push.scalar_problem(v) or push.check_list(v))   # a malformed one fails, so it is on
    except (TypeError, ValueError):
        return True


def _cycle_held(p: Project, cfg: dict | None) -> str:
    """The line of a power cycle of this host a device broker announced, or "" (cyclenotice.py)."""
    h = cyclenotice.held(cyclenotice.paths(cfg) if cfg else cyclenotice.project_paths(p))
    return cyclenotice.line(h, p) if h else ""


def _resume(p: Project, b: dict, marker: Path, now: float, free: bool = False) -> bool:
    """Run the after_push of a batch whose process died in it once more: the same
    `ttp push --batch <marker>`, which resumes at after_push since the marker has an outcome. It holds
    the batch's after_push lock (not `push:`, so upgrades do not wait for it), passed down as at the
    start, and the marker names it."""
    db = p.db
    lock_path = batch._after_lock(p, b["id"])
    lock = locks.try_take([lock_path], f"push batch {b['id']} after_push (resumed)", "the push queue")
    if lock is None:
        return False
    try:
        # One the batch itself held for a power-cycle notice (`free`) spends no resume.
        if not db.conn.execute("UPDATE push_batches SET after_tries=after_tries+? WHERE id=? AND (after_tries<? OR ?) "
                               "AND after_finalized IS NULL", (0 if free else 1, b["id"], MAX_RESUMES,
                                                               int(free))).rowcount:
            return False
        m = _read(marker)
        for k in ("after_held", "held_said"):
            m.pop(k, None)
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
    clears once after_push is unset, since no after_push will run to succeed. With the queue off,
    ordinary pushes still run it (tend_receipts)."""
    off = not _after_push_set(p, cfg)
    if bool(p.db.kv(AFTER_PUSH_OFF)) != off:
        p.db.set_kv(AFTER_PUSH_OFF, off)


def tend_receipts(p: Project, alert: Callable = lambda *a, **k: None, now: float | None = None) -> list[str]:
    """The tick's step for the after_push of ordinary and detached pushes (batch.after_ordinary_push):
    each receipt that ended, or whose process died in it (its lock is free while it reads running),
    becomes one event, and a failure also an after_push_failed alert; then it is marked reported.
    Reported receipts older than push.KEEP_S go. Returns the ids it reported."""
    now = time.time() if now is None else now
    folder = p.state / batch.RECEIPTS
    done = []
    for receipt in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        m = _read(receipt)
        if not m:
            continue
        if m.get("reported"):
            if now - float(m.get("reported") or now) > push.KEEP_S:
                receipt.unlink(missing_ok=True)
            continue
        rid = str(m.get("id") or receipt.stem)
        if m.get("status") == "rerun" and float(m.get("rerun_at") or 0) <= now - RERUN_START_S \
                and int(m.get("tries") or 0) >= MAX_RESUMES:
            m["status"] = "running"     # its rerun never got going: reported below as stopped
            write_json(receipt, m)
        if m.get("status") in ("held", "rerun") or (m.get("status") == "running" and locks.any_free(
                [batch._after_lock(p, rid)]) and _read(receipt).get("status") == "running"
                and m.get("boot") and not runner.same_boot(m.get("boot")) and int(m.get("tries") or 0) < MAX_RESUMES):
            _rerun_receipt(p, receipt, rid, now)      # held for a power cycle, or a reboot cut it short
            continue
        if m.get("status") == "running":
            if not locks.any_free([batch._after_lock(p, rid)]):
                continue
            m = _read(receipt)     # read after the lock: the process writes its outcome before letting go
            if m.get("status") == "running":
                m.update(status="finished", ended=now, after_push={
                    "status": "killed", "exit": None, "started": m.get("started"),
                    "tail": "the process ended before after_push finished (killed, crashed or rebooted)"})
        ap = m.get("after_push") if isinstance(m.get("after_push"), dict) else {}
        status = str(ap.get("status") or "failed")
        what = _short(m.get("pushed_sha")) + f" to {m.get('target') or '?'}" + (
            f" (task #{m['task']})" if m.get("task") else "")
        if status == "ok":
            took = float(ap.get("ended") or 0) - float(ap.get("started") or 0)
            _event(p.db, None, "after_push_ok", f"after_push of {what} succeeded"
                                                + (f" in {took:.0f} s" if took > 0 else ""), queued=False)
            p.db.set_kv(AFTER_PUSH_OK, max(now, time.time()))   # after an alert an older receipt raised this tick
        elif status != "skipped":
            how = {"failed": f"failed (exit {ap.get('exit')})", "timeout": "timed out",
                   "killed": "stopped before it finished"}.get(status, status)
            if ap.get("cmd") and status != "killed":
                how += f" in `{ap['cmd']}`"
            text = f"after_push of {what} {how}. The push stands. Receipt: {receipt}"
            _event(p.db, None, "after_push_failed", f"{text}\n{_tail_of(m, ap)}", queued=True, severity="high")
            if not (status == "failed" and _deploy_alerted(p, m, ap)):
                alert("after_push_failed", text, severity="high")
        m["reported"] = now
        write_json(receipt, m)
        done.append(rid)
    return done


def _rerun_receipt(p: Project, receipt: Path, rid: str, now: float) -> None:
    """An ordinary push's after_push that waited for a power-cycle notice (no resume spent) or that a reboot
    cut short (once, MAX_RESUMES): run it again, detached (batch.rerun_receipt), once no notice holds."""
    m = _read(receipt)
    if _cycle_held(p, None):
        return
    if m.get("status") == "rerun" and float(m.get("rerun_at") or 0) > now - RERUN_START_S:
        return          # its process is starting (or another tick started it): not twice
    if m.get("status") in ("running", "rerun"):
        m["tries"] = int(m.get("tries") or 0) + 1
        why = "a reboot cut it short"
    else:
        why = "the power-cycle notice it waited for is gone" if m.get("status") == "held" else "its rerun did not start"
    m.update(status="rerun", rerun_at=now)
    write_json(receipt, m)
    try:
        # own session: the rerun deploy outlives this tick, like the after_push run it replaces
        subprocess.Popen(batch.rerun_argv(p, receipt), cwd=str(p.root), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                              "TTP_PROJECT": str(p.base)})
    except OSError as e:
        _event(p.db, None, "after_push_failed", f"after_push of {_short(m.get('pushed_sha'))} could not run again: "
                                                f"{e}", queued=True, severity="high")
        return
    _event(p.db, None, "after_push_resumed", f"after_push of {_short(m.get('pushed_sha'))}: running it again "
                                             f"({why})", queued=False)


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
                # Only a batch that wrote nothing it could not finish: an unverified push still needs
                # the coordinator, reboot or not, and an error is the batch's own.
                cut = (_reboot_in(p, float(b["started"]), now_) if m and not m.get("unverified")
                       and m.get("outcome") != "error" else None)
                _died(p, b, m, alert, why, now_, queued=bool(m.get("unverified")), rebooted=cut)
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
        elif why := _cycle_held(p, cfg):     # busy, not a try: resumed once the notice is gone
            if not m.get("held_said"):
                _event(db, None, "after_push_held", f"push batch {b['id']}: its after_push waits: {why}",
                       queued=False)
                write_json(marker, {**m, "held_said": now_})
            continue
        elif m.get("after_held") or int(b["after_tries"] or 0) < MAX_RESUMES:
            if _resume(p, b, marker, now_, free=bool(m.get("after_held"))):
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
    if not db.one("SELECT id FROM push_queue WHERE status IN ('approved','batched','held') LIMIT 1") and not db.one(
            "SELECT id FROM push_batches WHERE after_finalized IS NULL LIMIT 1") and not db.kv(scopehold.KV):
        return []
    cancel_orphans(p)
    if may_requeue:
        queue_off(p, cfg)
    changed = finalize(p, cfg, alert)
    check_landing(p, cfg, alert)
    tend_scope_holds(p, cfg)
    return changed


def check_landing(p: Project, cfg: dict | None = None, alert: Callable = lambda *a, **k: None,
                  now: float | None = None) -> bool:
    """Raise LANDING_ALERT (high) once per stall: approvals have waited delivery.push_landing_alert_h
    (0 = off) since the last landing on the push branch and at least two batches tried them. It
    counts the batches and how many a reboot cut short; alerts.holds clears it once a batch lands.
    Returns whether it alerted now."""
    now = time.time() if now is None else now
    db = p.db
    hours = _num(_delivery(p, cfg).get("push_landing_alert_h"), DEFAULT_LANDING_ALERT_H, float, 0)
    oldest = db.one("SELECT MIN(created) AS t FROM push_queue WHERE status IN ('approved','batched')")
    if hours <= 0 or not oldest or oldest["t"] is None:
        return False
    paused = db.paused_resources()
    tgt = target(p)
    if "push" in paused or (tgt and push.lock_name(*tgt) in paused):
        return False   # paused: the queue is not retrying
    marks = ",".join("?" * len(LANDED_OUTCOMES))
    landed = db.one(f"SELECT MAX(finalized) AS t FROM push_batches WHERE outcome IN ({marks})", LANDED_OUTCOMES)
    since = max(float(oldest["t"]), float(landed["t"] or 0) if landed else 0.0)
    if now - since < hours * 3600:
        return False
    tries = db.q("SELECT outcome FROM push_batches WHERE started>=? AND finalized IS NOT NULL", (since,))
    st = _state(db)
    if len(tries) < 2 or float(st.get("landing_told") or 0) >= since:
        return False
    st["landing_told"] = now
    db.set_kv(KV, st)
    cut = sum(1 for r in tries if r["outcome"] == "rebooted")
    alert(LANDING_ALERT, f"Nothing has landed on {'/'.join(tgt) if tgt else 'the push branch'} for "
                         f"{(now - since) / 3600:.1f} h although approvals wait: {len(tries)} push batch"
                         f"{'es' if len(tries) != 1 else ''} tried, {cut} of them cut short by a host reboot. "
                         f"The queue keeps retrying; this clears once a batch lands.", severity="high")
    return True


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
    held = [{"id": r["id"], "task": r["task"], "branch": r["branch"], "scope": _detail(r).get("scope"),
             "age_s": round(now - r["updated"], 1)}
            for r in db.q("SELECT * FROM push_queue WHERE status='held' ORDER BY id")]
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
    return {"on": enabled(p), "approved": approved, "held": held, "live": live, "last": batches, "last_pushed": pushed,
            "backoff_until": st.get("backoff_until"), "bad_tip": st.get("bad_tip"), "deaths": int(st.get("deaths") or 0),
            "conflicts": conflict_stats(db, now - STATS_S), "conflict_fixes": conflictfix.stats(db, now - 86400)}


# What a live batch is doing, a finished batch's outcome and its after_push, in words for the user.
PHASE_WORDS = {"push": "checking", "after_push": "deploying", "finished": "finishing"}
OUTCOME_WORDS = {"pushed": "pushed", "landed": "already on the branch", "nothing": "nothing to push",
                 "conflict": "conflicted", "tip_failed": "the branch tip fails its checks", "moved": "the branch kept moving",
                 "busy": "another push held the branch", "rejected": "rejected by the remote", "refused": "refused",
                 "died": "ended before finishing", "rebooted": "cut short by a reboot", "error": "failed"}
AFTER_WORDS = {"ok": "deploy ok", "failed": "deploy failed", "timeout": "deploy timed out",
               "killed": "deploy cut short", "skipped": "no deploy"}


def _age(s: float) -> str:
    s = max(float(s), 0.0)
    return f"{int(s // 60)} min" if s < 7200 else f"{s / 3600:.0f} h" if s < 172800 else f"{s / 86400:.0f} days"


def _at(ts: float, now: float, where) -> str:
    return timefmt.short(ts, where, now)


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
    if held := sm.get("held"):
        scopes = ", ".join(f"`{x}`" for x in dict.fromkeys(h["scope"] or "?" for h in held))
        parts.append(f"{len(held)} held for the token scope {scopes}")
    if not n and not live and not held:
        parts.append("empty")
    last = sm["last"][0] if sm["last"] else None
    if last and last["outcome"] != "pushed" and not (live and live["id"] == last["id"]):
        parts.append(f"last batch {OUTCOME_WORDS.get(last['outcome'], last['outcome'])} "
                     f"{_age(now - (last['ended'] or last['started']))} ago")
    bad = sm.get("bad_tip") or {}
    wait = max(float(sm.get("backoff_until") or 0), _bad_until(bad) if bad.get("tip") else 0.0)
    if int(bad.get("fails") or 0) >= TIP_FAILS:
        parts.append(f"the branch tip {_short(bad['tip'])} fails its own checks")
    if wait > now and n and not live:
        parts.append(f"next try {_at(wait, now, p)}")
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
                  f"('approved','batched','held') OR q.batch IN ({','.join('?' * len(ids)) or 'NULL'}) "
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
    f = sm.get("conflict_fixes") or {}
    if f.get("fixes"):
        lines.append(f"last 24 h: {f['fixes']} conflict fix{'es' if f['fixes'] != 1 else ''}, ${f['usd']:.2f} "
                     f"(${f['usd_per_fix']:.2f} each); {f['light']} light (${f['light_usd']:.2f}), "
                     f"{f['other']} other (${f['other_usd']:.2f})")
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
