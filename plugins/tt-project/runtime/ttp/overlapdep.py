# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lanes that touch the same files line up behind each other and start early (the overlap lane).

Three model-free pieces, each with an off-switch under `overlap`:

- `auto_dependency` (setting `auto_depend`, default on): a new code task whose spec names a file an open
  code task also names gets that task in its `depends_on`, instead of running beside it and meeting the
  conflict at landing. Only one task is added, the newest open one that may be waited on (not one
  holding a device or waiting itself: overlap.exempt), never one in the new task's own lineage, and
  never an urgent task (the caller's rule: an urgent task keeps only safety waits).
- `early_start` / `start_early` (setting `early_deps`, default on): a queued code task whose only unmet
  dependency is a code task whose change is approved or batched in the push queue starts now, on that
  head, as a `start_when` of `landed:#N` does (lanewait): its dependency leaves `depends_on` and the task
  gets lanewait's `start_from:` label. Any other unmet dependency, a task that would not branch from
  that head (worktree.branches_early) or a dependency with no queue row keeps waiting for 'done'.
- `split_note` (setting `split_hours`, default 2): advice, never a refusal, for a spec whose history
  estimate (the median run of its kind and tier, or its cells times the median cell time) passes the
  setting: split it into lanes that can land alone."""
from __future__ import annotations

import json
import time

DEFAULTS = {"auto_depend": True, "early_deps": True, "split_hours": 2.0}
OPEN = ("queued", "running", "review", "pushing")


def settings(p) -> dict:
    raw = (p.config().get("overlap") or {}) if p is not None else {}
    out = dict(DEFAULTS)
    for k, d in DEFAULTS.items():
        v = raw.get(k)
        if isinstance(d, bool) and isinstance(v, bool):
            out[k] = v
        elif not isinstance(d, bool) and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = float(v)
    return out


def auto_dependency(p, kind: str, clash: list[dict], deps: list[int], roots: set = frozenset()) -> tuple[int, str] | None:
    """(id, why) of the open code task a new `kind` task with overlap hits `clash` (coordinator's
    _file_overlaps) waits for, None when it runs beside them: off, not a code task, no open hit that
    may be waited on, or the newest one is already a dependency."""
    if kind != "code" or not clash or not settings(p)["auto_depend"]:
        return None
    from . import chaincap
    lineage = set(roots) | {t["id"] for r in roots for t in chaincap.chain(p.db, r)}
    for h in clash:   # newest first
        if not h.get("open") or h.get("exempt") or h["id"] in lineage:
            continue
        t = p.db.task(h["id"])
        if not t or t["kind"] != "code" or t["status"] not in OPEN:
            continue
        if h["id"] in deps:
            return None
        paths = ", ".join(path for path, _ in h.get("paths", [])[:3])
        return h["id"], f"shares {paths or 'files'}"
    return None


def early_start(db, p, t: dict) -> tuple[int, int, str] | None:
    """(X, push queue row, head) when queued code task `t` waits on exactly one unmet dependency X, a code
    task whose change is approved or batched, and would branch from that head; else None."""
    from . import lanewait, worktree
    from .db import dependency_ids
    if t.get("status") != "queued" or t.get("kind") != "code" or not settings(p)["early_deps"]:
        return None
    ids = [d for d in dependency_ids(t) if d is not None]
    if not ids or "start_when:" in str(t.get("labels") or ""):
        return None
    unmet = [i for i in db.unmet_dependencies([t]).get(t["id"], []) if i is not None]
    if len(unmet) != 1:
        return None
    x = db.task(unmet[0])
    if not x or x["kind"] != "code" or not worktree.branches_early(p, t):
        return None
    return lanewait.batched(db, f"landed:#{unmet[0]}")


def start_early(db, p, now: float | None = None) -> list[int]:
    """Release every queued task `early_start` finds: its dependency leaves depends_on and the early
    labels go on (lanewait.early_labels). The ids released."""
    from . import lanewait
    from .db import dependency_ids
    out = []
    for t in db.q("SELECT * FROM tasks WHERE status='queued' AND kind='code' AND depends_on NOT IN ('', '[]')"):
        got = early_start(db, p, t)
        if not got:
            continue
        with db.tx():
            db.update_task(t["id"], depends_on=[d for d in dependency_ids(t) if d != got[0]],
                           labels=lanewait.early_labels(json.loads(t["labels"] or "[]"), got))
        out.append(t["id"])
    return out


def estimate_s(hist: dict, kind: str, tier: str | None, spec: str) -> float | None:
    """The history estimate of a task not run yet, seconds: its `cells:` lines times the kind's median
    seconds per cell, else the median run of its kind and tier (or the kind alone); None with no samples."""
    from . import cells
    from .eta import _med
    n = len(cells.requested(spec or ""))
    if n and (c := _med(hist.get("cell", {}).get(kind) or [])):
        return n * c
    xs = hist.get("run", {}).get((kind, tier)) or hist.get("kind", {}).get(kind) or []
    return _med(xs)


def split_note(p, kind: str, tier: str | None, spec: str, hist: dict | None = None) -> str:
    """Advice for a spec whose history estimate passes `split_hours`, "" when it does not (or has no history)."""
    limit = settings(p)["split_hours"]
    if kind != "code" or not limit:
        return ""
    if hist is None:
        from . import eta
        hist = eta.history(p.db, time.time())
    est = estimate_s(hist, kind, tier, spec)
    if est is None or est <= limit * 3600:
        return ""
    return (f"its history estimate is {est / 3600:.1f} h (over {limit:g} h): consider splitting it into lanes that "
            f"each land alone")
