"""One head gets one review. A review records, as it starts, the head commit it reviews and the
project's check set (a label); once it is accepted (`done`: a changes_needed verdict is stored
failed), a later review of the same head under the same checks reuses its verdict instead of
running again. The daemon looks before it queues or starts a review, and the coordinator's
task_add of such a review is refused unless forced with a reason. A different head, other checks
or an earlier review that failed or asked for changes still get a review. A review that would push
(or a code task's review in a project that pushes) takes only a verdict whose head was delivered:
a review-only verdict never pushed it, so reusing it would drop the delivery. A review that also
covers a head the accepted one does not contain (a rebase review naming the old head beside the
rebased branch) reuses it only when that head's commits past the base carry the same patches
(rebased_away)."""
from __future__ import annotations

import hashlib
import json
import re

HEAD_LABEL = "review_head:"     # review_head:<40-hex sha>@<check set key>
FORCED_LABEL = "review_forced"  # a review task_add forced past an accepted one: it runs
SUMMARY_CHARS = 600
_LABEL = re.compile(r"review_head:([0-9a-f]{40})@([0-9a-f]+)")


def check_key(cfg: dict) -> str:
    """A short key for the project's check set (delivery.push_checks, and push_checks_by_repo once
    set): other checks, other key."""
    from .push import checks_config
    checks = checks_config(cfg.get("delivery") or {})
    return hashlib.sha1(json.dumps(checks, sort_keys=True, default=str).encode()).hexdigest()[:12]


def head_label(sha: str, key: str) -> str:
    return f"{HEAD_LABEL}{sha}@{key}"


def recorded(task: dict) -> tuple[str, str] | None:
    """(head, check key) a review recorded as it started, or None."""
    for lb in json.loads(task.get("labels") or "[]"):
        m = _LABEL.fullmatch(lb) if isinstance(lb, str) else None
        if m:
            return m.group(1), m.group(2)
    return None


def with_head(labels: list, sha: str, key: str) -> list:
    """`labels` with the head label set to `sha` under `key` (a later start replaces the earlier one)."""
    return [lb for lb in labels if not (isinstance(lb, str) and lb.startswith(HEAD_LABEL))] + [head_label(sha, key)]


def pushes(cfg: dict, labels: list | tuple = ()) -> bool:
    """Whether a review with `labels` (a code task's review: no labels) would deliver its head to the
    push branch: the project pushes to one and the review is not labelled review only."""
    from .project import push_allowed
    from .push import LAND_LABEL, REVIEW_ONLY_LABEL
    d = cfg.get("delivery") or {}
    return push_allowed(d) and bool(str(d.get("push_branch") or "").strip()) \
        and (REVIEW_ONLY_LABEL not in labels or LAND_LABEL in labels)


def delivered(p, sha: str) -> bool:
    """Whether head `sha` is on its way or already there: a live push_queue row for it, a review that
    reviewed it and recorded a landing (`ttp land`), or the push target's tip as last fetched (a
    local branch in a code repo with no git remote) contains it."""
    from . import push, pushq
    if p.db.one("SELECT 1 FROM push_queue WHERE head=? AND status IN ('approved','batched','held','pushed',"
                "'landed') LIMIT 1", (sha,)):
        return True
    if p.db.one("SELECT 1 FROM task_landings l JOIN tasks t ON t.id=l.task WHERE t.kind='review' AND t.labels LIKE ? "
                "LIMIT 1", (f'%"{HEAD_LABEL}{sha}@%',)):
        return True
    tgt = pushq.target(p)
    tip = (push._local_tip(p.root, tgt[1]) if push.remote_less(p.root) else pushq._tracking_tip(p, *tgt)) if tgt else ""
    return bool(tip) and pushq._contains(p, [tip], sha)


def accepted(db, sha: str, key: str, skip: int | None = None, p=None) -> dict | None:
    """The review that first accepted `sha` under check set `key` (a review that reused one points at
    it), or None. Only a `done` review counts: failed, changes_needed and cancelled ones do not. With
    project `p` the caller would push `sha` (pushes): only a delivered head counts, as a review-only
    verdict stands in for no push."""
    if not sha or (p is not None and not delivered(p, sha)):
        return None
    for r in db.q("SELECT * FROM tasks WHERE kind='review' AND status='done' AND labels LIKE ? ORDER BY id",
                  (f'%"{head_label(sha, key)}"%',)):
        if r["id"] == skip:
            continue
        first = _result(r).get("reused_review")
        orig = db.task(first) if isinstance(first, int) else None
        return orig if orig and orig["status"] == "done" else r
    return None


def rebased_away(p, review: dict, sha: str) -> str:
    """Why `review` must not reuse the verdict on `sha`, or "": each other head it covers
    (worktree.covered_heads) that `sha` does not contain must carry the same commits past the base
    branch as `sha`, by git patch-id, in order. A changed patch, an extra or missing commit, a merge
    in either range, or a base or patch that cannot be read refuses reuse."""
    from . import worktree
    others = [h for h in worktree.covered_heads(p, review) if h != sha and not worktree.is_ancestor(p, h, sha)]
    if not others:
        return ""
    try:
        base = worktree.resolve_base(p)
        mine = _patches(p, base, sha)
        for h in others:
            if (theirs := _patches(p, base, h)) != mine:
                return (f"head {h[:12]}, which the review also covers, differs from {sha[:12]} past {base}: "
                        f"{_difference(mine, theirs)}")
    except Exception as e:
        return f"its covered heads were not compared with {sha[:12]}: {e}"
    return ""


def _patches(p, base: str, head: str) -> list[str]:
    """The patch-ids of `head`'s commits past `base`, oldest first. Raises on a merge in that range,
    whose patch has no patch-id to compare."""
    import subprocess
    from . import gitmemo, worktree
    if worktree._git(p.root, "rev-list", "--merges", f"{base}..{head}"):
        raise ValueError(f"{head[:12]} has a merge past {base}")
    commits = worktree._git(p.root, "rev-list", "--reverse", f"{base}..{head}").split()
    log = worktree._git(p.root, "log", "--reverse", "-p", "--no-color", "--format=commit %H", f"{base}..{head}")
    out = subprocess.run(["git", "-C", str(p.root), "patch-id", "--stable"], input=log + "\n",
                         capture_output=True, text=True, timeout=gitmemo.timeout(120), check=True).stdout
    ids = {c: pid for pid, c in (line.split()[:2] for line in out.splitlines() if line.strip())}
    return [ids.get(c, "empty") for c in commits]


def _difference(mine: list[str], theirs: list[str]) -> str:
    if len(mine) != len(theirs):
        return f"{len(theirs)} commit(s) against {len(mine)}"
    return "a changed patch"


def _result(task: dict) -> dict:
    try:
        v = json.loads(task.get("result") or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def rejection(prior: dict, sha: str) -> str:
    """Why task_add refuses a review of a head `prior` already accepted."""
    return (f"head {sha[:12]} was already accepted by review #{prior['id']} under the same checks: its verdict "
            f"stands (dependents can take it from #{prior['id']}); review a new head, or add force: true with "
            f"a reason saying why this head needs another review")


def reuse(db, review: dict, prior: dict, sha: str, key: str) -> str:
    """Close `review` with `prior`'s verdict, without a run: its result names `prior` and carries its
    summary, and each open task waiting on it gets that as the review's evidence in its spec. The
    event text, for the caller to post."""
    from .db import dependency_ids
    clip = str(_result(prior).get("summary") or "")[:SUMMARY_CHARS]
    summary = (f"Reused the verdict of review #{prior['id']}, which accepted head {sha[:12]} under the same "
               f"checks: no new run." + (f" Its summary: {clip}" if clip else ""))
    labels = with_head(json.loads(review.get("labels") or "[]"), sha, key)
    db.update_task(review["id"], status="done", labels=labels, blocked_reason=None,
                   result=json.dumps({"status": "done", "summary": summary, "reused_review": prior["id"],
                                      "reviewed_head": sha}))
    line = (f"\nReview #{review['id']} (which this waits on) reused the accepted verdict of review #{prior['id']} "
            f"on head {sha[:12]}; its evidence is review #{prior['id']}'s result.")
    for t in db.q("SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled') "
                  "AND depends_on NOT IN ('', '[]')"):
        if review["id"] in dependency_ids(t) and line.strip() not in (t["spec"] or ""):
            db.update_task(t["id"], spec=(t["spec"] or "") + line)
    return f"#{review['id']} {review['title']} → done (no run): {summary}"
