# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Hand-backs to Opus that are no user step.

A run under the GLM guard (codearm, glmwork) that asks for Opus or deep thinking hands its task back. Two
gaps closed here, model-free:

- `offroute_bounce`: a run whose GLM route fell back to a cheaper core model before it started (its endpoint's
  quota) still runs under the GLM guard, but ran off its route, so the daemon's bounce check (on the route only)
  missed its `needs_opus` and the task stood blocked. Such a run is a bounce too: requeued once on the core
  provider at Opus, with no attempt spent (route.BOUNCE). A task that already bounced once is left as it is.
- `need_line` and `refused`: a hand-off's machine reason ("needs Opus: guard: refusal 4 ...") is no step for
  the user. The chat's "Needs from you:" line says in plain words that the task needs another run and nothing
  is needed from the user, and a coordinator reply quoting such a line is refused."""
from __future__ import annotations

import re
from pathlib import Path

# A blocked reason that names a run's own hand-back, never a step for the user.
MACHINE_NEED = re.compile(r"^\W*(?:needs[ _](?:opus|deep(?: thinking)?)\b|guard\s*:|refusal \d)", re.I)
QUOTED = re.compile(r"needs from you\s*:\s*(.+)", re.I)


def offroute_bounce(db, cfg: dict, r: dict, routed: dict | None, run_dir: Path, result) -> str:
    """Why worker run `r` (its route entry `routed`, its hand-off `result`), which ran under the GLM guard on
    another provider than its route's (a fallback before it started), hands its task back to Opus; '' when not."""
    from . import codearm, glmguard, glmwork
    if not (routed and r.get("task") and r.get("role") == "worker") or r.get("provider") == routed.get("to"):
        return ""   # an on-route run is the daemon's own bounce check
    if "glm" not in (routed.get(codearm.KEY), routed.get(glmwork.KEY)) or not (Path(run_dir) / glmguard.RUN_MARK).exists():
        return ""
    if codearm.bounced(db, r["task"]):
        return ""   # once: a task that already bounced keeps its hand-off
    return glmwork.bounce_reason(result, cfg)


def machine_need(need) -> bool:
    """Whether a blocked reason is a run's own hand-back to Opus, not a step for the user."""
    return bool(MACHINE_NEED.search(str(need or "")))


def need_line(task_id, need) -> str:
    """The line a task's chat hand-off ends with for its blocked reason `need`."""
    need = str(need or "").strip()
    if not need:
        return ""
    if machine_need(need):
        return f"\nTask #{task_id} needs another run; nothing is needed from you."
    return f"\nNeeds from you: {need}"


def refused(text: str, what: str = "reply") -> str:
    """Why a coordinator `what` is refused for quoting a hand-off's 'Needs from you:' line whose need is no
    user step, else ''."""
    for m in QUOTED.finditer(str(text or "")):
        if machine_need(m.group(1)):
            return (f"{what} rejected: it quotes a hand-off's 'Needs from you: {m.group(1).strip()[:80]}' line, which "
                    f"is the run's own hand-back, not a step for the user; say in plain words what is true (for "
                    f"example: the task needs another run; nothing is needed from you) and send the {what} again")
    return ""
