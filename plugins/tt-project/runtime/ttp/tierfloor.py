# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
r"""The tier floor for conclusion runs: a run whose output is a conclusion the user reads never runs on
the light tier or below high effort.

Model-free, applied at each start after the effort pick (effort.py: Jev's or the rules' pick, a retry's
tier-up) and the coordinator's tier, so tasks already queued are lifted too. A task is a conclusion run
when any of these holds: its kind is `question` or `plan`; it answers a user chat (`reply_chat`); its
spec has a line starting `conclusion:`; or it is a run of an llm schedule whose output is posted to the
user (the daily review, or a schedule named for a review, summary, digest, report or brief). Such a run
gets tier max(its own, floor tier) and effort max(its own, floor effort), the nearest effort its
provider takes. The floor is coordinator.conclusion_floor (default DEFAULT: standard, high). A review
keeps its diff-sized tier; a task whose pin the user's own words require (pin_required) stays as it is.
A waiting conclusion task's wake whose wait is over may write the answer, so it runs at the floor too;
only a stale wake (the same wait handed back) stays a light check. A budget gate whose max_tier is
below the floor tier does not clamp a conclusion run: the run waits queued (HELD_NOTE) until the gate
allows the floor, so it spends nothing extra and the gate keeps its cap.

A lift that changes the stored tier moves the task's budget to the new tier's default when it held the
old tier's default (an explicit budget stays), sets tier_source `conclusion_floor` and adds one spec
note, e.g. '[tier floor] light->standard, effort low->high (conclusion: kind question)', which `ttp task
show`, the coordinator digest and the web app show.

Known gaps (accepted, settled area sa38):
1) tests/tt_project_runtime/tierfloor_cases.py::test_a_conclusion_runs_effort_floor_lifts_a_low_tier_effort_as_the_provider_takes_it
   checks only the effort passed to the provider build, not the runner spec's pin. Removing
   `pin["effort"] = lifted` in Daemon.start_run would not fail it.
2) LINE (^[ \t>*#\d.)-]*conclusion\s*:) also matches date- or number-prefixed lines such as
   '2026-11-02 conclusion:'. Over-inclusion only lifts the run to the floor (a small extra cost, no lost work).
3) The floor effort is resolved per provider (failover.kept_effort), not per pinned model, so a
   pinned model that takes no 'high' effort could be refused by the runner's pin check.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .failover import EFFORT_RANK, TIERS, kept_effort, required

KEY = "conclusion_floor"   # coordinator.conclusion_floor
DEFAULT = {"tier": "standard", "effort": "high"}
NOTE_PREFIX = "[tier floor]"
SOURCE = "conclusion_floor"   # tasks.tier_source
KINDS = ("question", "plan")
LINE = re.compile(r"(?im)^[ \t>*#\d.)-]*conclusion\s*:")
REPORT = re.compile(r"(?i)\b(review|summar(?:y|ies|ise|ize)|digest|reports?|brief(?:ing)?)\b")
DAILY_REVIEW = "daily-review"
HELD_NOTE = "held: tier floor waits for the budget gate to allow"   # + the floor tier (blocked_reason)


def floor_of(v: Any) -> dict:
    """coordinator.conclusion_floor as config_set takes it: {"tier", "effort"}, or a string such as
    'standard/high', 'deep xhigh' or 'standard' (effort: DEFAULT's). `light` with no effort turns the
    floor off. ValueError on anything else."""
    if isinstance(v, dict):
        tier, eff = v.get("tier"), v.get("effort", DEFAULT["effort"])
    elif isinstance(v, str):
        parts = [p for p in re.split(r"[\s/,:]+", v.strip().lower()) if p]
        if parts in (["off"], ["none"]):
            return {"tier": "light", "effort": ""}
        tier, eff = (parts + [None, None])[:2]
        if eff is None:
            eff = "" if tier == "light" else DEFAULT["effort"]
    else:
        raise ValueError(f"coordinator.{KEY} must be a tier and effort such as 'standard/high'; got {v!r}")
    tier, eff = str(tier or "").strip().lower(), str(eff or "").strip().lower()
    if tier not in TIERS:
        raise ValueError(f"coordinator.{KEY}: tier must be one of {', '.join(TIERS)}; got {tier!r}")
    if eff and eff not in EFFORT_RANK:
        raise ValueError(f"coordinator.{KEY}: effort must be one of {', '.join(EFFORT_RANK)} or empty; got {eff!r}")
    return {"tier": tier, "effort": eff}


def floor(cfg: dict) -> dict:
    """The project's floor; DEFAULT when unset or unreadable (it is on by default)."""
    v = (cfg.get("coordinator") or {}).get(KEY)
    if v is None:
        return dict(DEFAULT)
    try:
        return floor_of(v)
    except ValueError:
        return dict(DEFAULT)


def _labels(task: dict) -> list[str]:
    try:
        return [str(x) for x in json.loads(task.get("labels") or "[]")]
    except (TypeError, ValueError):
        return []


def reasons(task: dict) -> list[str]:
    """Why `task` is a conclusion run (empty: it is not)."""
    out = []
    if task.get("kind") in KINDS:
        out.append(f"kind {task['kind']}")
    if task.get("reply_chat"):
        out.append("answers a chat")
    if LINE.search(task.get("spec") or ""):
        out.append("conclusion: line")
    if task.get("origin") == "schedule":
        name = next(iter(_labels(task)), "")
        if name == DAILY_REVIEW or REPORT.search(name.replace("-", " ").replace("_", " ")):
            out.append(f"schedule {name}")
    return out


def _rank(effort: str | None) -> int:
    e = (effort or "").strip().lower()
    return EFFORT_RANK.index(e) if e in EFFORT_RANK else -1   # unset: the provider's own default, below any floor


def target(db, cfg: dict, task: dict) -> dict | None:
    """The floor ({tier, effort}) `task` must start at, or None when it is no conclusion run, the floor is
    off, it is a review or the user's own words require its pin."""
    if task.get("kind") == "review" or not reasons(task):
        return None
    fl = floor(cfg)
    if fl["tier"] == "light" and not fl["effort"]:
        return None
    if required(db, task):
        return None   # the user's own words require its pin
    return fl


def lift(db, cfg: dict, task: dict, provider: str) -> dict | None:
    """The floor for this start of `task` on `provider`, or None when none applies or nothing would move:
    {tier, from, effort (the run's effort floor, or None), effort_from, why}."""
    fl = target(db, cfg, task)
    if not fl:
        return None
    why = reasons(task)
    own = task.get("tier") if task.get("tier") in TIERS else "standard"
    tier = max(own, fl["tier"], key=TIERS.index)
    tiers = ((cfg.get("providers") or {}).get(provider) or {}).get("tiers") or {}
    before = str(task.get("effort") or (tiers.get(tier) or {}).get("effort") or "")
    want = kept_effort(provider, fl["effort"]) if fl["effort"] else None
    effort = want if want and _rank(before) < _rank(want) else None
    if tier == own and not effort:
        return None
    was = str(task.get("effort") or (tiers.get(own) or {}).get("effort") or "")
    return {"tier": tier, "from": own, "effort": effort, "effort_from": was, "why": why}


def note(got: dict) -> str:
    """The spec note: '[tier floor] light->standard, effort low->high (conclusion: kind question)'."""
    parts = [f"{got['from']}->{got['tier']}"] if got["tier"] != got["from"] else [f"tier {got['tier']}"]
    if got.get("effort"):
        parts.append(f"effort {got.get('effort_from') or 'default'}->{got['effort']}")
    return f"{NOTE_PREFIX} {', '.join(parts)} (conclusion: {'; '.join(got['why'])})"


def apply(db, cfg: dict, task: dict, got: dict) -> dict:
    """Store a lift on the task: its tier (and budget, when it held the old tier's default) and the spec
    note, once per distinct lift. Returns the task as it now starts."""
    from .budget import task_default_usd
    upd: dict = {}
    if got["tier"] != task.get("tier"):
        upd.update(tier=got["tier"], tier_source=SOURCE)
        held = task.get("budget_usd")
        if held is not None and abs(float(held) - task_default_usd(cfg, task.get("tier"))) < 0.005:
            upd["budget_usd"] = task_default_usd(cfg, got["tier"])
    text = note(got)
    spec = task.get("spec") or ""
    if text not in spec:
        upd["spec"] = f"{spec}\n\n{text}" if spec else text
    if upd:
        db.update_task(task["id"], **upd)
    return {**task, **upd}
