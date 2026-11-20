# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Hold a daemon-queued review whose head carries another change that is still being fixed.

A code task's head can carry commits of another task whose latest review failed (pushq.carried).
While a fix or re-review of that task is still open, its reviewer could only repeat the open
finding, so the daemon queues the review deferred (db.deferral) instead of dispatching it:
- start_when `landed:#<carried task>` (one probe per carried task, all must pass), so the existing
  probe machinery starts it once the fix lands; its labels keep the hold across daemon restarts;
- start_why names the carried task and its open fix, which the review's status line shows;
- the label `carried_hold:<head>:<id>[,<id>...]` marks it as this hold, for release().

release() (the daemon's model-free pass) also ends the hold once every open chain's change is approved
for the push queue (an approved or batched row) at a head the held head contains: the review then
covers the base's approved commits with the stacked ones and need not wait for the landing; a base
whose approved head is not in the held head (it changed since) keeps the hold. It also ends the hold
when every carried task's fix chain ended without landing (its last task failed or was cancelled: the review spec then says so) or at
review.carried_hold_max_h hours (0: no cap). On any release the spec's line on inherited commits is
refreshed: a carried task whose fix landed is no longer named."""
from __future__ import annotations

import json
import time
from typing import Callable

from . import landed
from .db import TERMINAL_TASK_STATES, deferral, dependency_ids, without_deferral

LABEL = "carried_hold:"
MAX_H = 24.0   # review.carried_hold_max_h: the longest a review is held
INHERITED_PREFIX = "Its head carries unreviewed commits"   # daemon.inherited_line


def chain_state(db, tid: int, skip: set[int] = frozenset()) -> tuple[str, int | None]:
    """The fix chain after task `tid`'s latest failed review (landed.carriers, leaving out `skip`):
    ("open", task) while one of its tasks is not finished; ("done" | "failed" | "cancelled", last task)
    once all are; ("none", None) when no review of it failed or nothing follows the failed one."""
    ids = set(landed.carriers(db, tid))
    # A review is in the chain by what it depends on too (a coordinator's review has no auto_review label).
    ids |= {r["id"] for r in db.q("SELECT id, depends_on FROM tasks WHERE kind='review' AND id>?", (tid,))
            if set(dependency_ids(r)) & ids}
    rows = [t for i in sorted(ids) if i not in skip and (t := db.task(i))]
    failed = max((t["id"] for t in rows if t["kind"] == "review" and t["status"] == "failed"), default=None)
    after = [t for t in rows if failed is not None and t["id"] > failed]
    if not after:
        return "none", None
    if (o := next((t for t in after if t["status"] not in TERMINAL_TASK_STATES), None)) is not None:
        return "open", o["id"]
    return after[-1]["status"], after[-1]["id"]


def blocking(db, carried, skip: set[int] = frozenset()) -> dict[int, int]:
    """{carried task: its open fix or re-review} for each of `carried` whose fix chain is open."""
    out = {}
    for tid in sorted(carried):
        state, last = chain_state(db, int(tid), set(skip))
        if state == "open":
            out[int(tid)] = last
    return out


def labels(head: str, held: dict[int, int]) -> list[str]:
    """The deferral labels that hold a review of `head` for `held` (see blocking); [] when nothing holds."""
    if not held:
        return []
    ids = sorted(held)
    probe = f"landed:#{ids[0]}" if len(ids) == 1 else " && ".join(f"ttp landed --task {i}" for i in ids)
    why = "; ".join(f"#{t}'s fix #{f} lands or its chain ends" for t, f in sorted(held.items()))
    return [f"start_when:{probe}", f"start_why:{why}", f"deferred_since:{time.time():.0f}",
            f"{LABEL}{head}:{','.join(map(str, ids))}"]


def hold_of(task: dict) -> tuple[str, list[int]] | None:
    """(head, carried task ids) of a review held by labels(); None for any other task."""
    try:
        lbs = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return None
    for lb in lbs if isinstance(lbs, list) else []:
        if isinstance(lb, str) and lb.startswith(LABEL):
            head, _, ids = lb[len(LABEL):].partition(":")
            return head, [int(i) for i in ids.split(",") if i.isdigit()]
    return None


def approved_row(db, tid: int) -> dict | None:
    """The newest approved or batched push queue row of task `tid`'s change (it or a task carrying it
    on, landed.carriers); None while there is none (a held row, missing a token scope, does not count)."""
    ids = sorted(set(landed.carriers(db, tid)) | {tid})
    return db.one(f"SELECT id, task, head FROM push_queue WHERE status IN ('approved','batched') AND head IS NOT NULL "
                  f"AND task IN ({','.join('?' * len(ids))}) ORDER BY id DESC LIMIT 1", tuple(ids))


def verdict(db, task: dict, now: float, max_h: float,
            contains: Callable[[str], bool] | None = None) -> tuple[str, str] | None:
    """None while the held review `task` stays held; else (why, spec line) to release it: "landed"
    (its start_when passed), "approved" (every open chain's change is approved for the push queue at a
    head the held head contains: `contains(head)`, a git ancestry check), "ended" (no carried fix chain
    is open any more; the line names those that ended failed or cancelled) or "cap" (held max_h hours,
    a chain still open)."""
    h = hold_of(task)
    d = deferral(task)
    if h is None:
        return None
    if not d.get("when"):
        return "landed", ""
    skip = {task["id"], *(i for i in dependency_ids(task) if i is not None)}
    states = {t: chain_state(db, t, skip) for t in h[1]}
    open_ = {t: last for t, (s, last) in states.items() if s == "open"}
    if open_ and contains is not None:
        rows = {t: approved_row(db, t) for t in open_}
        if all(r is not None and contains(r["head"]) for r in rows.values()):
            return "approved", " ".join(
                f"Carried task #{t}'s fix is approved for the push queue at {r['head'][:12]} (row {r['id']}, #{r['task']}) "
                f"and this head contains it: review the head whole, those approved commits with this task's "
                f"own; they are not this task's to deliver." for t, r in sorted(rows.items()))
    if open_:
        since = d.get("since") or task.get("created") or now
        if max_h and now - since >= max_h * 3600:
            return "cap", (f"This review was held {(now - since) / 3600:.0f} h for the fix of "
                           + ", ".join(f"#{t} (#{f} still open)" for t, f in sorted(open_.items()))
                           + ", the longest it waits: judge the carried commits as the line on them says.")
        return None
    ended = [(t, s, last) for t, (s, last) in sorted(states.items()) if s in ("failed", "cancelled")]
    line = " ".join(f"The fix chain of carried task #{t} ended {s} (#{last}) without landing: its commits "
                    f"on this head are still unreviewed; judge them as the line on them says." for t, s, last in ended)
    return "ended", line


def released_labels(task: dict) -> list:
    lbs = json.loads(task.get("labels") or "[]")
    return [lb for lb in without_deferral(lbs) if not (isinstance(lb, str) and lb.startswith(LABEL))]


def respec(spec: str, inherited: str, why: str) -> str:
    """`spec` with its line on inherited commits replaced by `inherited` (dropped when empty) and
    `why` after it. Without such a line, both go before the closing "Return the verdict" line."""
    lines = spec.split("\n")
    new = [x for x in (inherited, why) if x]
    at = next((i for i, x in enumerate(lines) if x.startswith(INHERITED_PREFIX)), None)
    if at is not None:
        return "\n".join(lines[:at] + new + lines[at + 1:])
    end = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("Return the verdict")), len(lines))
    return "\n".join(lines[:end] + new + lines[end:])
