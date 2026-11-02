# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Effort A/B for standard-tier code tasks, and Jev's pick of a code task's effort level.

With `effort_ab.enabled` on (on by default; an explicit false keeps it off; chat-settable), each code task queued at standard with no
effort pin of its own goes in one of two arms, picked by a hash of its lineage's root task id
(chaincap.chain): `high` keeps the tier's effort, `medium` runs at medium effort. Its retries, review
fixes and continuations share the root, so they keep its arm. The arm changes only a standard-tier run
whose tier effort is high: a light run, or a deep one after a tier-up retry, keeps its tier's effort but
still records the arm, so the report counts its cost. Each run of an arm task carries the arm in its
note (`effort_ab`: arm, root, `effort` the arm or pick gave it, `effective` the effort it started at).
A conclusion run's tier floor (tierfloor.py) may lift `effective` above `effort` (`lifted`): the report
leaves out every lineage with such a run, and a lifted Jev pick is settled as no change.

`report` compares the arms per lineage (one change): cost per landed change, output tokens and model
calls per run, review and checks fail rates, fix-review hops and tier-up retries. End rule (END_*):
make medium the default only once each arm has at least END_MIN_LANDED landed changes and medium's
review and checks fail rates are each no more than END_MAX_WORSE_PTS points above high's. `conclude`
applies it: once the rule decides, it records the winner (`effort_ab.winner`, `decided_at`) and turns
the split off; a `medium` winner then runs every task the A/B would have covered at medium effort
(`{"by": "winner"}` in the run note). Thinking tokens per arm come from the run note's
`thinking_tokens`, which the providers fill from the result usage when it breaks them out.

With `effort_ab.jev_pick` on (off until the A/B result is in), a standard-tier code task outside the
A/B has Jev score its spec once (its lineage keeps the pick): medium or high, else the tier's effort
when Jev is off or gives no answer. The pick goes in the run note and in jev_calls (use `effort`),
which effort.settle resolves once the task ends.
"""
from __future__ import annotations

import hashlib
import json
import time

from . import chaincap, jevuse
from .db import DB, assigned_tier, load_result, review_subject

KEY = "effort_ab"            # project.json section, and the run note's entry
CONTROL, TEST = "high", "medium"
ARMS = (CONTROL, TEST)
END_MIN_LANDED = 60          # landed changes each arm needs before the end rule decides
END_MAX_WORSE_PTS = 3.0      # medium's review and checks fail rates may be at most this many points worse
JEV_CRITERIA = ["a routine change that medium reasoning effort handles well",
                "a tricky change that needs high reasoning effort"]
FAILED_REVIEWS = ("failed", "changes_needed")


def _on(cfg: dict, key: str) -> bool:
    v = (cfg.get(KEY) or {}).get(key, False)
    return v is True or str(v).lower() in ("1", "true", "yes", "on")


def enabled(cfg: dict) -> bool:
    return _on(cfg, "enabled")


def jev_pick_on(cfg: dict) -> bool:
    return _on(cfg, "jev_pick")


def winner(cfg: dict) -> str | None:
    """The effort the end rule picked for this project (conclude), or None while none was picked."""
    w = (cfg.get(KEY) or {}).get("winner")
    return w if w in ARMS else None


def _root(db: DB, task: dict) -> dict:
    ch = chaincap.chain(db, task["id"]) if task.get("id") else []
    return ch[0] if ch else task


def arm_of(root_id: int) -> str:
    """The arm of lineage root `root_id`: stable across processes and hosts (no salted hash())."""
    return ARMS[hashlib.sha256(f"effort_ab:{root_id}".encode()).digest()[0] % 2]


def eligible(db: DB, task: dict) -> dict | None:
    """The lineage root of a code task the A/B or Jev's pick may cover, or None: a review, a task with
    its own effort pin, or one whose lineage was queued at another tier than standard."""
    if task.get("kind") != "code" or str(task.get("effort") or ""):
        return None
    root = _root(db, task)
    if assigned_tier(root) != "standard" or str(root.get("effort") or ""):
        return None
    return root


def _ran_effort(tier: str, tier_effort: str, effort: str) -> str:
    """The effort a run at `tier` with tier effort `tier_effort` takes from an arm or pick of `effort`."""
    return effort if tier == "standard" and tier_effort == CONTROL else tier_effort


def _earlier_pick(db: DB, ids: list[int]) -> dict | None:
    """Jev's recorded pick from an earlier run of the lineage, so a retry or a hop never asks again."""
    if not ids:
        return None
    rows = db.q("SELECT note FROM runs WHERE task IN (%s) AND role!='coordinator' AND note LIKE ? ORDER BY id"
                % ",".join("?" * len(ids)), (*ids, f'%"{KEY}"%'))
    for r in rows:
        got = load_result(r["note"]).get(KEY)
        if isinstance(got, dict) and got.get("by") == "jev" and got.get("pick") in ARMS:
            return got
    return None


def _effort_cost(db: DB, cfg: dict, provider: str, model: str, effort: str) -> float | None:
    row = db.one("SELECT AVG(cost_usd) c, COUNT(*) n FROM runs WHERE role='worker' AND provider=? AND "
                 "COALESCE(model,'')=? AND COALESCE(effort,'')=? AND status!='running' AND cost_usd>0 AND started>=?",
                 (provider, model, effort, time.time() - jevuse.window_s(cfg)))
    return float(row["c"]) if row and row["n"] else None


def _jev_pick(db: DB, cfg: dict, task: dict, root: dict, provider: str, model: str, jev) -> dict | None:
    if jev is None or not jev.enabled() or not jevuse.allowed(db, cfg, "effort"):
        return None
    state = f"kind: {task.get('kind')}\ntitle: {root.get('title')}\nspec:\n{(root.get('spec') or '')[:6000]}"
    ans = jev.decide(state, {"effort": {"type": "score", "criteria": JEV_CRITERIA,
                                        "instructions": "How much reasoning effort does this change need?"}},
                     purpose="effort", timeout=10.0)
    if ans is None:
        return None
    try:
        score = float((ans.get("effort") or {}).get("score"))
    except (TypeError, ValueError):
        return None
    pick = TEST if score < 0.5 else CONTROL
    avoided = 0.0
    if pick == TEST:
        hi, med = _effort_cost(db, cfg, provider, model, CONTROL), _effort_cost(db, cfg, provider, model, TEST)
        avoided = max(0.0, hi - med) if hi is not None and med is not None else 0.0
    cid = jevuse.record(db, "effort", {"effort_level": pick, "score": score, "root": root["id"]},
                        getattr(jev, "last_cost", 0.0), avoided_usd=avoided, ref=f"task:{task['id']}",
                        changed=pick != CONTROL)
    return {"by": "jev", "pick": pick, "score": score, "jev_call": cid}


def assign(db: DB, cfg: dict, task: dict, tier: str, provider: str, jev=None) -> dict | None:
    """The run note's `effort_ab` entry for this start of `task` at `tier` on `provider`, or None:
    {"arm", "root", "effort"} in the A/B, {"by": "jev", "pick", "effort", ...} from Jev's pick.
    `effort` is what the run takes (start_run applies it; never over a task's own pin)."""
    if not (enabled(cfg) or jev_pick_on(cfg) or winner(cfg) == TEST):
        return None
    root = eligible(db, task)
    if root is None:
        return None
    t = ((cfg.get("providers") or {}).get(provider) or {}).get("tiers", {}).get(tier) or {}
    tier_effort, model = str(t.get("effort") or ""), str(t.get("model") or "")
    if enabled(cfg):
        arm = arm_of(root["id"])
        return {"arm": arm, "root": root["id"], "effort": _ran_effort(tier, tier_effort, arm)}
    if not jev_pick_on(cfg):   # the A/B's winner: medium (a high winner keeps the tier's effort)
        return {"by": "winner", "root": root["id"], "effort": _ran_effort(tier, tier_effort, TEST)}
    ids = [x["id"] for x in chaincap.chain(db, task["id"])] if task.get("id") else []
    got = _earlier_pick(db, ids)
    if got is None:
        try:
            got = _jev_pick(db, cfg, task, root, provider, model, jev)
        except Exception as e:   # picking must never hold a task back
            got = {"by": "tier", "jev_error": type(e).__name__}
    if not got or got.get("pick") not in ARMS:
        return {**(got or {}), "by": "tier", "root": root["id"], "effort": tier_effort}
    return {**{k: got[k] for k in ("by", "pick", "score", "jev_call") if k in got}, "root": root["id"],
            "effort": _ran_effort(tier, tier_effort, got["pick"])}


def ran(db: DB, ab: dict, effective: str) -> dict:
    """The run note's `effort_ab` entry once start_run settled the effort the run starts at: `effective`,
    and `lifted` when a conclusion run's tier floor raised it over the arm's or pick's `effort`. A lifted
    Jev pick never ran: its call settles as no change (it avoided nothing, and is scored neither way)."""
    out = {**ab, "effective": effective}
    if str(effective or "") != str(ab.get("effort") or ""):
        out["lifted"] = True
        if ab.get("jev_call"):
            jevuse.resolve(db, int(ab["jev_call"]), jevuse.NO_CHANGE,
                           f"tier floor ran it at {effective}, not {ab.get('effort')}")
    return out


def _rate(bad: int, n: int) -> float | None:
    return round(100.0 * bad / n, 1) if n else None


def report(p, days: float = 30, since: float | None = None, until: float | None = None) -> dict:
    """Per arm: the lineages (changes) whose runs carry it in the window, and what they cost and how
    they went. `checks fail rate`: runs that ran `ttp checks` whose last recorded result failed. A lineage
    with a run the tier floor lifted (`lifted`) is left out: it did not run at its arm's effort."""
    from . import landed
    from .prguard import CHECKS_FILE
    db = p.db
    until = time.time() if until is None else until
    since = until - days * 86400 if since is None else since
    rows = db.q("SELECT r.id, r.task, r.cost_usd, r.output_tokens, r.note, t.labels, t.title, t.tier_source "
                "FROM runs r JOIN tasks t ON t.id=r.task WHERE r.role='worker' AND r.status!='running' "
                "AND r.started>=? AND r.started<? AND r.note LIKE ? ORDER BY r.id", (since, until, f'%"{KEY}"%'))
    arms: dict[str, dict] = {a: {"changes": set(), "tasks": set(), "runs": 0, "usd": 0.0, "output": 0, "thinking": 0,
                                 "thinking_runs": 0, "calls": [],
                                 "checks": 0, "checks_failed": 0, "tier_ups": 0, "fix_hops": set()} for a in ARMS}
    notes = [(r, load_result(r["note"])) for r in rows]
    lifted = {int((n.get(KEY) or {}).get("root") or r["task"]) for r, n in notes
              if isinstance(n.get(KEY), dict) and n[KEY].get("lifted")}
    for r, note in notes:
        ab = note.get(KEY)
        if not isinstance(ab, dict) or ab.get("arm") not in ARMS or int(ab.get("root") or r["task"]) in lifted:
            continue
        a = arms[ab["arm"]]
        a["changes"].add(int(ab.get("root") or r["task"]))
        a["tasks"].add(r["task"])
        a["runs"] += 1
        a["usd"] += float(r["cost_usd"] or 0)
        a["output"] += int(r["output_tokens"] or 0)
        thinking = note.get("thinking_tokens")
        if isinstance(thinking, int) and not isinstance(thinking, bool) and thinking >= 0:
            a["thinking"] += thinking
            a["thinking_runs"] += 1
        calls = (note.get("cache_telemetry") or {}).get("calls")
        if isinstance(calls, int) and calls > 0:
            a["calls"].append(calls)
        if (note.get("pick") or {}).get("by") == "retry":
            a["tier_ups"] += 1
        if chaincap.is_review_fix({"labels": r["labels"], "title": r["title"]}):
            a["fix_hops"].add(r["task"])
        try:
            rec = json.loads((p.runs / str(r["id"]) / CHECKS_FILE).read_text())
        except (OSError, ValueError):
            rec = None
        if isinstance(rec, dict) and "passed" in rec:
            a["checks"] += 1
            a["checks_failed"] += not rec["passed"]
    reviews = db.q("SELECT s.outcome, t.labels, t.title FROM review_stats s JOIN tasks t ON t.id=s.task "
                   "WHERE s.ts>=? AND s.ts<?", (since, until))
    out: dict = {"since": since, "until": until, "arms": {}}
    for name, a in arms.items():
        rv = [r["outcome"] for r in reviews
              if review_subject({"labels": r["labels"], "title": r["title"]}) in a["tasks"]]
        landed_n = sum(1 for root in a["changes"] if landed.recorded(db, root)[0])
        n = a["runs"]
        out["arms"][name] = {
            "changes": len(a["changes"]), "landed": landed_n, "runs": n, "usd": round(a["usd"], 2),
            "usd_per_landed": round(a["usd"] / landed_n, 2) if landed_n else None,
            "output_per_run": round(a["output"] / n) if n else None,
            # None: no run of the arm had a provider that breaks thinking tokens out of its output
            "thinking_tokens": a["thinking"] if a["thinking_runs"] else None, "thinking_runs": a["thinking_runs"],
            "calls_per_run": round(sum(a["calls"]) / len(a["calls"]), 1) if a["calls"] else None,
            "reviews": len(rv), "review_fail_pct": _rate(sum(o in FAILED_REVIEWS for o in rv), len(rv)),
            "checks": a["checks"], "checks_fail_pct": _rate(a["checks_failed"], a["checks"]),
            "fix_hops": len(a["fix_hops"]), "fix_hops_per_change": round(len(a["fix_hops"]) / len(a["changes"]), 2)
            if a["changes"] else None, "tier_ups": a["tier_ups"]}
    out["verdict"] = verdict(out["arms"])
    return out


def verdict(arms: dict) -> str:
    """The end rule's call on the report's arms: 'collecting', 'medium' or 'high'."""
    hi, med = arms.get(CONTROL) or {}, arms.get(TEST) or {}
    if min(hi.get("landed") or 0, med.get("landed") or 0) < END_MIN_LANDED:
        return "collecting"
    for k in ("review_fail_pct", "checks_fail_pct"):
        if (med.get(k) or 0.0) - (hi.get(k) or 0.0) > END_MAX_WORSE_PTS:
            return CONTROL
    return TEST


def report_text(r: dict) -> str:
    def f(v, unit=""):
        return "-" if v is None else f"{v}{unit}"
    lines = ["effort A/B (standard-tier code tasks): high vs medium"]
    for name in ARMS:
        a = r["arms"][name]
        lines.append(f"  {name:<6} {a['changes']} changes, {a['landed']} landed, {a['runs']} runs, ${a['usd']:.2f}; "
                     f"${f(a['usd_per_landed'])}/landed change, {f(a['output_per_run'])} output tokens/run, "
                     f"{f(a['calls_per_run'])} calls/run, " + (
                         f"{a['thinking_tokens']} thinking tokens ({a['thinking_runs']} of {a['runs']} runs report them)"
                         if a["thinking_tokens"] is not None else "thinking tokens n/a") + "; " +
                     f"review fail {f(a['review_fail_pct'], '%')} of {a['reviews']}, "
                     f"checks fail {f(a['checks_fail_pct'], '%')} of {a['checks']}; {a['fix_hops']} fix-review hops "
                     f"({f(a['fix_hops_per_change'])}/change), {a['tier_ups']} tier-up retries")
    v = r["verdict"]
    lines.append(f"  end rule ({END_MIN_LANDED}+ landed changes per arm, medium's review and checks fail rates "
                 f"within {END_MAX_WORSE_PTS:g} points): " +
                 {"collecting": "still collecting", TEST: "medium holds: make medium the standard effort",
                  CONTROL: "medium fails more: keep high"}[v])
    if r.get("winner"):
        lines.append(f"  decided: {r['winner']} is this project's standard effort for code tasks; the split is off")
    return "\n".join(lines)


def conclude(p, cfg: dict, now: float | None = None) -> str | None:
    """Apply the end rule while the A/B runs: once it decides, record the winner (effort_ab.winner,
    decided_at) and turn the split off. Returns the winner it recorded, else None. Counts runs since an
    earlier decision only, so an A/B switched on again starts from nothing."""
    if not enabled(cfg):
        return None
    now = time.time() if now is None else now
    since = float((cfg.get(KEY) or {}).get("decided_at") or 0)
    got = verdict(report(p, since=since, until=now)["arms"])
    if got == "collecting":
        return None
    p.set_config(f"{KEY}.winner", got)
    p.set_config(f"{KEY}.decided_at", now)
    p.set_config(f"{KEY}.enabled", False)
    return got
