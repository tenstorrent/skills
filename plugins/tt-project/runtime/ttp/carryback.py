# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Send a code task back once when its head carries another task's rejected commits.

A code task built on another task's branch hands off a head that also holds that task's commits. If
that task's latest review failed (pushq.carried), the push queue refuses any approval of the head, so
a review of it can only pass to be refused, then sleep. At the code hand-off the daemon instead sends
the task back once (Daemon._finish_worker, KEY in its result) with an order: rebase onto the push
branch's tip keeping only the commits it needs, or, when it needs that task's code, hand off waiting
on `landed:#<id>` with the reason. Carried commits pass with no send-back when:
- the task declared that wait: `landed:#<id>` (or `ttp landed --task <id>`) in its start_when, spec
  or its last waiting hand-off (chaincap.waits_landing);
- they are on the push branch already (pushq.carried leaves out landed and rebased copies);
- that task's change is approved for the push queue (reviewhold.approved_row);
- the task re-works that task's own change (reworks): it continues it, or the failed review of it, or
  delivers onto its branch (`pr_branch:`). Its commits are the change under re-work, not a stack.
After the one send-back the review hold (reviewhold) handles what is still carried, as before."""
from __future__ import annotations

from . import reviewhold
from .chaincap import waits_landing
from .db import continues_id, deferral, dependency_ids, review_subject
from .worktree import carried_branch

KEY = "carried_sendback"   # in the task's result: {"run", "tasks"} of its one send-back


def declared(task: dict, prev: dict, tid: int) -> bool:
    """Whether `task` (its start_when, spec, or its previous result `prev`'s wait) waits on task
    `tid`'s landing."""
    prev = prev if isinstance(prev, dict) else {}
    return any(waits_landing(s, tid) for s in (deferral(task).get("when"), task.get("spec"),
                                                prev.get("retry_when"), prev.get("waiting_for")))


def reworks(db, task: dict, tid: int) -> bool:
    """Whether `task` re-works task `tid`'s own change: down its `continues:` chain it reaches `tid` or
    a review of it, or it delivers onto `tid`'s branch (`pr_branch:`)."""
    other = db.task(tid)
    if other and other.get("branch") and carried_branch(task) == other["branch"]:
        return True
    seen, t = set(), task
    while t and t["id"] not in seen and len(seen) < 20:
        seen.add(t["id"])
        if t["id"] == tid or (t.get("kind") == "review"
                              and tid in {review_subject(t), *(d for d in dependency_ids(t) if isinstance(d, int))}):
            return True
        t = db.task(c) if (c := continues_id(t)) else None
    return False


def pending(db, task: dict, carried: dict, prev: dict) -> dict[int, list[str]]:
    """The part of `carried` ({task id: [commit lines]}, pushq.carried) that sends `task` back."""
    return {tid: lines for tid, lines in sorted((carried or {}).items())
            if tid != task["id"] and lines and not declared(task, prev, tid)
            and reviewhold.approved_row(db, tid) is None and not reworks(db, task, tid)}


def order(found: dict[int, list[str]], branch: str) -> str:
    """The send-back's words to the worker."""
    named = "; ".join(f"#{tid}: " + ", ".join(lines[:5]) + (f" and {len(lines) - 5} more" if len(lines) > 5 else "")
                      for tid, lines in found.items())
    ids = ", ".join(f"#{tid}" for tid in found)
    first = next(iter(found))
    return (f"your branch carries commits of {ids}, whose latest review failed and which are not on "
            f"{branch or 'the push branch'} ({named}). The push queue refuses a head carrying them, so a "
            f"review of it cannot land. Either rebase onto the tip of {branch or 'the push branch'} keeping "
            f"only the commits your change needs, then hand off done again; or, if your change needs that "
            f"code, hand off `waiting` with `retry_when` `ttp landed --task {first}` (one per task) and say "
            f"why in `waiting_for`. This is asked once")
