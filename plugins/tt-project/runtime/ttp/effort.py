# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Each task's effort (its tier) and whether it gets a review, picked once from its spec when it first
starts, and raised one tier on a retry after a failed run: to deep only after a failed standard run.

Defaults: standard (high effort). A task the coordinator queued at standard may start at light when
its spec is a short lookup: Jev scores it where its `effort` use is allowed (see jevuse), else simple
rules do. Neither picks deep on a first try: deep (max effort) comes only from whoever queued the task
(the user asked) or from a retry after a failed or hand-off-less standard run, and that run stays within
the task's own remaining budget, which is never raised. A device task fails from drops and reboots, not
too little effort: it goes deep only when a run hands off `needs_deep`, and one the coordinator queued at
deep starts at standard unless the user asked for deep (`user_deep`) or it continues a `needs_deep` try. Every code task keeps its review (the daemon
queues it); the pick only records it.

The pick is logged in the run's note, next to its outcome, and a Jev pick in jev_calls with the measured
cost it avoided; `settle` marks it right when the task ends done on its first try.
"""
from __future__ import annotations

import json
import re
import time

from . import jevuse, machines
from .db import DB, dump_result

JEV_USE = "effort"
SHORT_SPEC = 800    # characters: a longer spec is not a lookup
RETRY_FAILED = ("failed", "no_handoff")   # the previous run's outcomes a deep retry follows
RAISED = "effort_raised_at"   # in the task's result: the attempt count its failed try was already raised at
LOOKUP = re.compile(r"(?i)\b(check|look ?up|find|list|show|status|what|which|where|whether|how many|count|"
                    r"report|summari[sz]e|read|confirm|verify)\b")
CHANGES = re.compile(r"(?i)\b(fix|implement|change|edit|write|refactor|add|remove|delete|update|deploy|push|"
                     r"upgrade|install|build|migrate|design|debug|optimi[sz]e|rewrite|commit)\b")
CRITERIA = ["a lookup, status check or question with a short, factual answer",
            "normal engineering: changes, debugging or analysis of ordinary difficulty",
            "architecture, hard debugging or novel optimization"]


def rules_tier(task: dict) -> str:
    """Light for a short question, or short work that only looks something up; standard otherwise."""
    text = f"{task.get('title') or ''}\n{task.get('spec') or ''}"
    if len(task.get("spec") or "") > SHORT_SPEC or CHANGES.search(text):
        return "standard"
    if task.get("kind") == "question" or (task.get("kind") == "work" and LOOKUP.search(text)):
        return "light"
    return "standard"


def _labels(task: dict) -> list[str]:
    try:
        return [str(x) for x in json.loads(task.get("labels") or "[]")]
    except (TypeError, ValueError):
        return []


UP = {"light": "standard", "standard": "deep"}
NEEDS_DEEP = "needs_deep"   # hand-off key: the problem a standard run could not solve at its tier
USER_DEEP = "user_deep"     # label: the user asked for deep, so a device task keeps it on its first start


def _resource_names(task: dict) -> list[str]:
    return [lb.split(":", 1)[1] for lb in _labels(task) if lb.startswith(("resource:", "exclusive:"))]


def device_task(task: dict) -> bool:
    """Tagged `needs_device`, or names a `*-device` resource or a machine tagged `device`."""
    if "needs_device" in _labels(task):
        return True
    names = _resource_names(task)
    if any(n.endswith("-device") for n in names):
        return True
    if not names:
        return False
    try:
        known = machines.load()
        return any("device" in machines.tag_list((known.get(n) or {}).get("tags")) for n in names)
    except Exception:   # an unreadable machines list must never hold a task back
        return False


def wants_deep(result) -> bool:
    """The hand-off sets `needs_deep` to a non-empty reason."""
    if isinstance(result, str):
        try:
            result = json.loads(result or "{}")
        except (TypeError, ValueError):
            return False
    return isinstance(result, dict) and bool(str(result.get(NEEDS_DEEP) or "").strip())


def _continued(db: DB, task: dict) -> list[dict]:
    out = []
    for label in _labels(task):
        if label.startswith("continues:") and label[10:].isdigit():
            old = db.task(int(label[10:]))
            if old:
                out.append(old)
    return out


def retry_tier(db: DB, task: dict) -> str | None:
    """One tier up when this start retries a light or standard try that failed (its last run handed off
    `failed` or ended without a hand-off, or the task continues one that failed): deep only after a
    failed standard try, and for a device task only when that try handed off `needs_deep`. None otherwise. A failed try raises the tier once: a start that is requeued
    without spending an attempt keeps the failed result, so `mark_raised` notes the raise in it."""
    up = UP.get(task.get("tier") or "")
    if task.get("kind") == "review" or not up:
        return None
    try:
        last = json.loads(task.get("result") or "{}")
    except (TypeError, ValueError):
        last = {}
    attempts = int(task.get("attempts") or 0)
    gated = up == "deep" and device_task(task)
    if attempts and isinstance(last, dict) and last.get("status") in RETRY_FAILED:
        if gated and not wants_deep(last):
            return None
        return up if last.get(RAISED) != attempts else None
    for old in _continued(db, task):
        if old["status"] == "failed" and old["tier"] == task["tier"] and not (gated and not wants_deep(old["result"])):
            return up
    return None


def mark_raised(task: dict) -> str | None:
    """The task's result noting that its failed try has been raised, or None when the raise did not
    come from its result (a continued task starts at its raised tier, which already stops a second raise)."""
    try:
        last = json.loads(task.get("result") or "{}")
    except (TypeError, ValueError):
        return None
    attempts = int(task.get("attempts") or 0)
    if not attempts or not isinstance(last, dict) or last.get("status") not in RETRY_FAILED:
        return None
    return dump_result({**last, RAISED: attempts})


def tier_cost(db: DB, cfg: dict, provider: str, tier: str) -> float | None:
    """The mean cost of a finished worker run at `tier`'s model and effort over the Jev window."""
    t = ((cfg.get("providers") or {}).get(provider) or {}).get("tiers", {}).get(tier) or {}
    row = db.one("SELECT AVG(cost_usd) c, COUNT(*) n FROM runs WHERE role='worker' AND provider=? AND "
                 "COALESCE(model,'')=? AND COALESCE(effort,'')=? AND status!='running' AND cost_usd>0 AND started>=?",
                 (provider, str(t.get("model") or ""), str(t.get("effort") or ""), time.time() - jevuse.window_s(cfg)))
    return float(row["c"]) if row and row["n"] else None


def _jev_tier(db: DB, cfg: dict, task: dict, provider: str, jev) -> dict | None:
    """Jev's pick (light or standard) and its logged call, or None when Jev is off or gave no answer."""
    if jev is None or not jev.enabled() or not jevuse.allowed(db, cfg, JEV_USE):
        return None
    state = f"kind: {task.get('kind')}\ntitle: {task.get('title')}\nspec:\n{(task.get('spec') or '')[:6000]}"
    ans = jev.decide(state, {"effort": {"type": "score", "criteria": CRITERIA,
                                        "instructions": "How much reasoning effort does this task need?"}},
                     purpose="effort", timeout=10.0)
    if ans is None:
        return None
    try:
        score = float((ans.get("effort") or {}).get("score"))
    except (TypeError, ValueError):
        score = None
    # Light only on a clear score; never deep on a first try.
    tier = "light" if score is not None and score < 0.5 else "standard"
    review = task.get("kind") == "code"
    avoided = 0.0
    if tier != task["tier"]:
        given, picked = tier_cost(db, cfg, provider, task["tier"]), tier_cost(db, cfg, provider, tier)
        avoided = max(0.0, given - picked) if given is not None and picked is not None else 0.0
    cid = jevuse.record(db, JEV_USE, {"effort": tier, "review": review, "spec_len": len(task.get("spec") or ""),
                                      "score": score}, getattr(jev, "last_cost", 0.0), avoided_usd=avoided,
                        ref=f"task:{task['id']}", changed=tier != rules_tier(task))
    return {"tier": tier, "by": "jev", "score": score, "jev_call": cid}


def pick(db: DB, cfg: dict, task: dict, provider: str, jev=None) -> dict | None:
    """The tier this start of `task` runs at, with how it was picked, or None to leave the task as it
    is. Picks on the first start of a non-review task the coordinator queued at standard, and on a
    retry after a failed light or standard try. A device task the coordinator queued at deep starts at
    standard unless the user asked for deep or it continues a try that handed off `needs_deep`. The caller stores `tier` on the task and the dict in the run note."""
    if task.get("kind") == "review":
        return None
    review = task.get("kind") == "code"
    up = retry_tier(db, task)
    if up:
        return {"tier": up, "from": task["tier"], "by": "retry", "review": review}
    first = task.get("origin") == "coordinator" and not int(task.get("attempts") or 0) \
        and not db.one("SELECT 1 FROM runs WHERE task=? AND role!='coordinator' LIMIT 1", (task["id"],))
    if task.get("tier") == "deep" and first and USER_DEEP not in _labels(task) and device_task(task) \
            and not any(wants_deep(old["result"]) for old in _continued(db, task)):
        return {"tier": "standard", "from": "deep", "by": "device", "review": review}
    if task.get("tier") != "standard" or task.get("origin") != "coordinator" or int(task.get("attempts") or 0):
        return None
    if db.one("SELECT 1 FROM runs WHERE task=? AND role!='coordinator' LIMIT 1", (task["id"],)):
        return None   # picked when it first started
    try:
        got = _jev_tier(db, cfg, task, provider, jev)
    except Exception as e:   # picking must never hold a task back
        got = {"jev_error": type(e).__name__}
    if not got or "tier" not in got:
        got = {**(got or {}), "tier": rules_tier(task), "by": "rules"}
    return {**got, "from": task["tier"], "review": review}


def settle(db: DB, task_id: int, status: str, attempts: int) -> None:
    """Mark the task's Jev effort pick right once it ends done on its first try, wrong once it ends done
    after a retry or fails for good. Blocked and waiting tasks are not settled yet."""
    if status not in ("done", "failed"):
        return
    right = status == "done" and attempts <= 1
    for r in db.q("SELECT id FROM jev_calls WHERE use=? AND ref=? AND outcome IS NULL", (JEV_USE, f"task:{task_id}")):
        jevuse.resolve(db, r["id"], right, f"task {status} after {attempts} attempt(s)")
