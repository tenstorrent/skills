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



def own_landing_wait(task: dict, result: dict, cfg: dict, job_in_flight: bool = False) -> str:
    """Why a waiting hand-off is refused: a code task whose retry_when or waiting_for is its own landing.
    '' when it is fine (another task's landing, a push or checks probe, a detached job in flight, or
    not a code task). A later step of `then` may name the landing: it follows the task's own push."""
    if job_in_flight or not isinstance(result, dict) or not isinstance(task, dict) or task.get("kind") != "code":
        return ""
    if not any(waits_landing(result.get(k), task["id"]) for k in ("retry_when", "waiting_for")):
        return ""
    way = ("land it now with `ttp push --detach` (delivery.code_tasks_may_push is on) and wait on the `retry_when` "
           "it prints" if code_tasks_may_push(cfg) else
           "hand off `done` or `needs_review` so its review queues and lands it")
    return (f"hand-off refused: it waits on its own landing (task #{task['id']}), which cannot happen before it "
            f"finishes: the review that lands a change queues only once the task ends, so the wait never ends. "
            f"Instead {way}.")


def own_push_probe(p, task_id: int) -> str:
    """The `ttp push --result` probe of task `task_id`'s newest detached push still running or queued,
    '' with none: a code task that ran `ttp push --detach` and then waits on its own landing waits on
    that push, which ends (pushed or not) by itself."""
    from . import push
    mine = [m for m in push.running(p) if str(m.get("task") or "") == str(task_id)]
    return push.result_probe(Path(mine[-1]["marker"]), p) if mine else ""
