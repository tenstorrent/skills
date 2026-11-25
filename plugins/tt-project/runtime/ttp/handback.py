# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Hand-backs to Opus.

A run under the GLM guard (codearm, glmwork) that asks for Opus or deep thinking hands its task back. Model-free:

- `offroute_bounce`: a run whose GLM route fell back to a cheaper core model before it started (its endpoint's
  quota) still runs under the GLM guard, but ran off its route, so the daemon's bounce check (on the route only)
  missed its `needs_opus` and the task stood blocked. Such a run is a bounce too: requeued once on the core
  provider at Opus, with no attempt spent (route.BOUNCE). A task that already bounced once is left as it is."""
from __future__ import annotations

from pathlib import Path


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

