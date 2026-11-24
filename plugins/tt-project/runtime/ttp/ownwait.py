# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-free handling of a task's wait on its own checks or push: no model wake for work the daemon can
settle itself.

- report_only: a hand-off's `next_step` that only says to report the pass or the landing ("report the
  push", "record the result and finish") asks a model for nothing, so that wait settles model-free
  (daemon.push_on_pass, daemon.checks_settle_fill, checksettle.settle) as one with no next_step does.
  Anything else in the step (a rebase, a PR, a retry, a second action) keeps the model wake.
- own_landing_wait: a code task's waiting hand-off whose own wait is its own landing (`landed:#<id>`
  or `ttp landed --task <id>` of itself) can never end by itself: nothing lands the task before it
  finishes, because its review queues only then. The daemon refuses it (Daemon._finish_worker) with
  the way out: land with `ttp push` when delivery.code_tasks_may_push is on, else finish. While the
  task's own `ttp push --detach` is still running or queued, its wait is taken on that push instead
  (own_push_probe): the landing it waits for is under way.
- stack_ancestor_wait: a code task whose wait is the landing of an unlanded task in its own stack (it
  continues or delivers onto that task's branch, or its branch holds that task's head) cannot end either:
  its review queues only once it hands off, and the stack lands through the head's review. The daemon
  takes that hand-off as done, so the head's review approves the stack, and finishes a task already
  asleep on such a wait the same way when its probe is next due (Daemon._stack_wait_done).
"""
from __future__ import annotations

import re
from pathlib import Path

from .chaincap import waits_landing
from .project import code_tasks_may_push

# One short report-only sentence: a verb to report, what is reported, and nothing after it.
_VERB = r"(?:report|record|post|note|confirm|summari[sz]e|state|say)"
_NOUN = r"(?:push|landing|landed|pass|passed|checks?|result|results|outcome|status|commit|head|batch|hash|sha)"
_WHAT = rf"(?:the |its |this |that |a )?{_NOUN}(?: (?:and )?(?:the )?{_NOUN}){{0,3}}"
_END = r"(?:finish|stop|end|done|complete)"
_AFTER = rf"(?: (?:(?:and|then) {_END}|{_END}|to the coordinator|here))?"
REPORT_RE = re.compile(rf"^{_VERB} {_WHAT}{_AFTER}[.!]?$")
MAX_STEP_CHARS = 80


def report_only(step) -> bool:
    """True when `step` (a hand-off's next_step) only asks to report what happened: one short
    sentence, no second action. Empty or non-text is no step and returns False."""
    if not isinstance(step, str):
        return False
    text = " ".join(step.lower().split())
    if not text or len(text) > MAX_STEP_CHARS or any(c in text for c in ";:\n&|`$()"):
        return False
    return bool(REPORT_RE.fullmatch(text))


def names_step(result: dict) -> bool:
    """Whether a waiting hand-off names a next_step that needs a model (one that is not report_only)."""
    step = result.get("next_step") if isinstance(result, dict) else None
    return bool(step) and not report_only(step)



def own_landing_wait(task: dict, result: dict, cfg: dict, job_in_flight: bool = False,
                     self_land: bool | None = None) -> str:
    """Why a waiting hand-off is refused: a code task whose retry_when or waiting_for is its own landing.
    '' when it is fine (another task's landing, a push or checks probe, a detached job in flight, or
    not a code task). A later step of `then` may name the landing: it follows the task's own push."""
    if job_in_flight or not isinstance(result, dict) or not isinstance(task, dict) or task.get("kind") != "code":
        return ""
    if not any(waits_landing(result.get(k), task["id"]) for k in ("retry_when", "waiting_for")):
        return ""
    way = ("land it now with `ttp push --detach` (delivery.code_tasks_may_push is on) and wait on the `retry_when` "
           "it prints" if (code_tasks_may_push(cfg) if self_land is None else self_land) else
           "hand off `done` or `needs_review` so its review queues and lands it")
    return (f"hand-off refused: it waits on its own landing (task #{task['id']}), which cannot happen before it "
            f"finishes: the review that lands a change queues only once the task ends, so the wait never ends. "
            f"Instead {way}.")


def stack_ancestor_wait(p, task: dict, result: dict, job_in_flight: bool = False) -> int:
    """The id of the unlanded task in `task`'s own stack that its waiting hand-off waits on, 0 when it
    does not. Only a plain landing wait counts (`landed:#<id>`, `ttp landed --task <id>`, an `&&` chain
    of them, a trailing `|| exit 1` aside): another term in it is a real second condition. A task is in
    the stack when it re-works that task (carryback.reworks) or its branch holds that task's head."""
    if job_in_flight or not isinstance(result, dict) or not isinstance(task, dict) or task.get("kind") != "code":
        return 0
    from . import landed
    for key in ("retry_when", "waiting_for"):
        probe = result.get(key)
        if not isinstance(probe, str) or not probe.strip():
            continue
        probe = re.sub(r"\s*\|\|\s*exit\s+1\s*$", "", probe.strip())
        ids, rest = landed.split_landed(probe)
        if not ids or rest or task["id"] in ids:
            continue
        if all(_in_stack(p, task, tid) and landed.check(p, p.root, tid=tid, fetch=False)[0] != 0 for tid in ids):
            return ids[0]
    return 0


def _in_stack(p, task: dict, tid: int) -> bool:
    """Whether `task` re-works task `tid` or its branch contains `tid`'s head commit, even when `tid`'s
    latest review failed: those commits then reach the push branch only through this branch's review.
    Not while a fix chain of that failed review is open: its landing lands `tid` (landed.carriers), so
    carryback.order's wait on it ends."""
    from . import carryback, reviewhold
    if carryback.reworks(p.db, task, tid):
        return True
    other = p.db.task(tid) or {}
    mine, theirs = task.get("branch"), other.get("branch")
    if not (mine and theirs) or reviewhold.chain_state(p.db, tid, {task["id"]})[0] == "open":
        return False
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(p.root), "merge-base", "--is-ancestor", theirs, mine],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def own_push_probe(p, task_id: int) -> str:
    """The `ttp push --result` probe of task `task_id`'s newest detached push still running or queued,
    '' with none: a code task that ran `ttp push --detach` and then waits on its own landing waits on
    that push, which ends (pushed or not) by itself."""
    from . import push
    mine = [m for m in push.running(p) if str(m.get("task") or "") == str(task_id)]
    return push.result_probe(Path(mine[-1]["marker"]), p) if mine else ""
