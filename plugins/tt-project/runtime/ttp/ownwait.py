# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-free handling of a task's wait on its own checks or push: no model wake for work the daemon can
settle itself.

- report_only: a hand-off's `next_step` that only says to report the pass or the landing ("report the
  push", "record the result and finish") asks a model for nothing, so that wait settles model-free
  (daemon.push_on_pass, daemon.checks_settle_fill, checksettle.settle) as one with no next_step does.
  Anything else in the step (a rebase, a PR, a retry, a second action) keeps the model wake.
"""
from __future__ import annotations

import re

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

