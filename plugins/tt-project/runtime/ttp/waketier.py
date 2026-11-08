# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Why a waiting task woke, and the tier its run takes, decided before any model turn starts.

Every wake of a waiting task records its reason (KEY in the hand-off; KINDS): what changed. The
dispatched run's note keeps it with the decision (`wake`: reason, tier, by), so `ttp stats --wakes`
counts task wakes and their $ per day by reason.

The wait-wake gate (waitgate.py) decides whether a wake runs a model at all. This module decides the
tier of an escalated wake that does run (the hand-off asked for the task's own tier):

- FIX reasons (a failed check, job, push or on_pass_cmd, a broken probe, a light wake that asked for
  more): the work is known, so the run keeps the tier its wake set. Rule, no call.
- JUDGE reasons (a spec change that slept with the wait): it may only add context. Jev, where its
  `wake_tier` use is allowed (jevuse) and it is reachable, says whether the run needs judgment; when
  it is confident it does not (score below `waiting.wake_tier.jev_light_below`), the run wakes at
  light and as a plain wake, so it may still ask for its own tier (retry_after_s 0 and a higher
  wake_tier) once it finds real work. Jev off, unreachable or unsure: the wake's tier, as before.
- Anything else: the wake's tier.

A light pick books as avoided the measured mean cost of a wake at its from-tier less a light wake
(tier_cost; LIGHT_SHARE of jevuse's fallback price per tier step while either is unmeasured), so
jevuse.review keeps the use on while it pays. Each Jev call is settled when its run ends: a light pick
whose run asked for more is wrong, and its avoided cost becomes minus the light run it added.

Check plans (PLAN in the hand-off): a waiting hand-off with a retry_when gets planned checks, so its
model looks at the wait a known number of times, not on every timer. By default one interim check soon
after the wait starts (did it start, does it run, no early errors) and one at its expected end, the
measured median wait of its kind (wait_kind: own checks, a detached job, a push, another probe;
record_wait keeps them as probes pass, END_FALLBACK_S while fewer than `min_samples` are measured). In
between, only a changed probe answer wakes it (the probe loop and waitgate). Each check wakes at the
plan's tier, `by`: "model" (always a run), "jev" (the wake gate's Jev step judges it, its mechanical
"still pending" rules skipped) or "plain" (the whole wake gate: a job still running is no turn). The
coordinator's task_update `check_plan` (PLAN_SET: checks, at_s, tier, by) overrides the defaults for
that task's waits; `waiting.check_plan` holds the defaults the daily review tunes (plan_lines).
"""
from __future__ import annotations

import re
import statistics
import time
from pathlib import Path

from . import budget as bud
from . import jevuse
from .db import DB

KEY = "wake_reason"
USE = "wake_tier"
PICK = "wake_pick"            # the hand-off's record of a lowered tier, copied into the run note
FIX = frozenset({"checks_failed", "tests_failed", "job_failed", "match_fail", "push_unlanded",
                 "on_pass_failed", "probe_broken", "asked", "underuse"})
JUDGE = frozenset({"spec_change"})
KINDS = FIX | JUDGE | {"timer", "probe_passed", "probe_changed", "wake_now", "checks_gone", "plan_check",
                       "stale_release", "reboot", "other"}
PLAN, PLAN_SET, PLAN_BY, PLAN_TIER = "check_plan", "check_plan_set", "plan_by", "plan_tier"
BYS = ("jev", "plain", "model")
PLAN_DEFAULTS = {"enabled": True, "interim_share": 0.15, "interim_min_s": 120.0, "interim_max_s": 900.0,
                 "tier": "light", "by": "jev", "min_samples": 3}
END_FALLBACK_S = {"checks": 1800.0, "job": 3600.0, "push": 900.0, "probe": 3600.0}
WAITS_KV, WAITS_KEEP = "wait_durations", 40
DEFAULT_LIGHT_BELOW = 0.3
LIGHT_SHARE = 0.25            # a light wake's price as a share of a standard one while unmeasured
JEV_TIMEOUT_S = 10.0


def reason(prev: dict) -> str:
    """The recorded reason of a due wake (hand-off `prev`): its KEY, else 'timer' when nothing woke
    it but its clock, else 'other'."""
    got = prev.get(KEY) if isinstance(prev, dict) else None
    if got in KINDS:
        return got
    return "other" if isinstance(prev, dict) and prev.get("woke") else "timer"


def _light_below(cfg: dict) -> float:
    try:
        return float(((cfg.get("waiting") or {}).get("wake_tier") or {}).get("jev_light_below",
                                                                          DEFAULT_LIGHT_BELOW))
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_LIGHT_BELOW


def tier_cost(db: DB, cfg: dict, tier: str, now: float | None = None) -> float | None:
    """The mean cost of a finished task wake at `tier` over jevuse's window (its run note's wake), or
    None while none was measured."""
    import json as _json
    now = time.time() if now is None else now
    got = []
    for r in db.q("SELECT cost_usd, note FROM runs WHERE role!='coordinator' AND status!='running' AND cost_usd>0 "
                  "AND started>=? AND note LIKE '%\"wake\"%' ORDER BY id DESC LIMIT 500", (now - jevuse.window_s(cfg),)):
        try:
            if (_json.loads(r["note"] or "{}").get("wake") or {}).get("tier") == tier:
                got.append(float(r["cost_usd"]))
        except (ValueError, TypeError, AttributeError):
            continue
    return sum(got) / len(got) if got else None


def _fallback(tier: str) -> float:
    """An unmeasured wake's price: jevuse's fallback for standard, LIGHT_SHARE of it per tier below,
    and as much again per tier above."""
    step = bud.TIER_ORDER.index(tier) - bud.TIER_ORDER.index("standard") if tier in bud.TIER_ORDER else 0
    return jevuse.WAKE_COST_FALLBACK_USD * (LIGHT_SHARE ** -step if step < 0 else 1 + step)


def avoided(db: DB, cfg: dict, tier: str, now: float | None = None) -> float:
    """What a light wake saves against one at `tier`: their measured mean costs, else the fallbacks."""
    hi, lo = tier_cost(db, cfg, tier, now), tier_cost(db, cfg, bud.TIER_ORDER[0], now)
    return max(0.0, (hi if hi is not None else _fallback(tier)) - (lo if lo is not None else _fallback(bud.TIER_ORDER[0])))


def pick(db: DB, cfg: dict, task: dict, prev: dict, jev=None, now: float | None = None) -> dict | None:
    """For an escalated wake of a JUDGE reason above light: {"tier": "light", "by", "call", "from"} when
    the chat watcher answers a light wake (or skip), or, with no watcher answer, Jev is confident the run
    needs no judgment; else None (the wake keeps its tier)."""
    if not isinstance(prev, dict) or not prev.get("escalated_wake") or reason(prev) not in JUDGE:
        return None
    tier = bud.wake_tier(task.get("tier") or "standard", prev, cfg)
    if tier is None or tier == bud.TIER_ORDER[0]:
        return None
    if jev is None or not jev.enabled():
        return None
    now = time.time() if now is None else now
    state = "\n".join([
        f"task: #{task['id']} {task.get('title') or ''}",
        f"why it woke: {str(prev.get('woke') or '')[:600]}",
        f"its spec now (the change is usually at the end): {str(task.get('spec') or '')[-2500:]}",
        f"its hand-off: {str(prev.get('summary') or '')[:800]}",
        f"what it waited for: {str(prev.get('waiting_for') or '')[:400]}",
        f"next step it named: {str(prev.get('next_step') or '')[:300]}",
    ])
    try:   # the chat watcher first: its verb's tier (light, or more) decides; it costs nothing
        if (ask := getattr(jev, "decision", None)) and (d := ask(
                state, "A waiting task woke because its spec changed. Wake it at light when the change only adds "
                       "context or notes and a check or report is all that is left; at standard when the change "
                       "asks for new or different work or the wait's outcome needs a decision.",
                target=str(task["id"]), purpose=USE)):
            light = bool(d.get("parsed")) and (d["verb"] == "skip" or (d["verb"] == "wake" and d.get("tier") == bud.TIER_ORDER[0]))
            call = jevuse.record(db, USE, {"task": task["id"], "by": "babysit", "model": d.get("model"),
                                           "verb": d["verb"], "tier": d.get("tier"), "light": light}, 0.0,
                                 avoided(db, cfg, tier, now) if light else 0.0, ref=f"task:{task['id']}",
                                 changed=light, settle_s=None, now=now)
            return {"tier": bud.TIER_ORDER[0], "by": "babysit", "model": d.get("model"), "call": call,
                    "from": tier} if light else None
    except Exception:   # the tier stays: an error never lowers a run
        return None
    if hasattr(jev, "decision"):   # the watcher logged why it did not answer: Jev itself, never a re-ask
        jev = jev.jev
        if jev is None or not jev.enabled():
            return None
    if not jevuse.allowed(db, cfg, USE):
        return None
    try:
        ans = jev.decide(state=state, questions={"judgment": {
            "type": "noul",
            "instructions": "A waiting task woke because its spec changed. Does its next run need real "
                            "judgment (new work, a changed plan, a fix), rather than only reading the change "
                            "and checking or reporting what it waited on?",
            "criteria": {"true": "The change asks for new or different work, or the wait's outcome needs a decision.",
                         "false": "The change only adds context or notes; a check or report is all that is left."}},
        }, purpose=USE, timeout=JEV_TIMEOUT_S)
        score = float((ans or {}).get("judgment", {}).get("noul", 1.0))
    except Exception:   # the tier stays: an error never lowers a run
        return None
    light = score < _light_below(cfg)
    call = jevuse.record(db, USE, {"task": task["id"], "judgment": round(score, 2), "light": light},
                         getattr(jev, "last_cost", 0.0), avoided(db, cfg, tier, now) if light else 0.0,
                         ref=f"task:{task['id']}", changed=light,
                         settle_s=None, now=now)
    if not light:
        return None
    return {"tier": bud.TIER_ORDER[0], "by": "jev", "call": call, "from": tier, "judgment": round(score, 2)}


def lowered(prev: dict, got: dict) -> dict:
    """The hand-off after a light pick: a plain light wake (not escalated, so its run may still ask for more)."""
    out = {k: v for k, v in prev.items() if k != "escalated_wake"}
    out.update({"wake_tier": got["tier"], PICK: got})
    return out


def settle(db: DB, note: dict, result: dict, now: float | None = None) -> None:
    """Resolve the Jev call behind a run's light pick: wrong when the run asked for more (a waiting
    hand-off with retry_after_s 0 and a higher wake_tier), else right."""
    got = ((note or {}).get("wake") or {}).get("pick") if isinstance(note, dict) else None
    if not isinstance(got, dict) or not got.get("call"):
        return
    more = (isinstance(result, dict) and result.get("status") == "waiting"
            and str(result.get("retry_after_s")) in ("0", "0.0") and result.get("wake_tier") in bud.TIER_ORDER[1:])
    if jevuse.resolve(db, int(got["call"]), not more, "its light run asked for more" if more else "", now) and more:
        # the light run it added is extra cost: a negative saving still counts against the use
        lo = tier_cost(db, {}, bud.TIER_ORDER[0], now)
        db.x("UPDATE jev_calls SET avoided_usd=? WHERE id=?",
             (-(lo if lo is not None else _fallback(bud.TIER_ORDER[0])), int(got["call"])))


def stats(db: DB, tz: str, days: float = 7, now: float | None = None) -> dict:
    """Task wakes per home-zone day by reason: runs, $ and how many only handed the wait back."""
    import json as _json
    from . import timefmt
    now = time.time() if now is None else now
    out: dict = {}
    for r in db.q("SELECT started, cost_usd, note, dir FROM runs WHERE role!='coordinator' AND started>=? "
                  "AND note LIKE '%\"wake\"%'", (now - days * 86400,)):
        try:
            note = _json.loads(r["note"] or "{}")
        except ValueError:
            continue
        wake = note.get("wake") if isinstance(note, dict) else None
        if not isinstance(wake, dict):
            continue
        day = timefmt.today(r["started"], tz)
        row = out.setdefault(day, {}).setdefault(str(wake.get("reason") or "unrecorded"),
                                                 {"wakes": 0, "usd": 0.0, "rewaited": 0, "by": {}})
        row["wakes"] += 1
        row["usd"] = round(row["usd"] + float(r["cost_usd"] or 0), 4)
        by = f"{wake.get('by') or 'rule'}/{wake.get('tier') or '?'}"
        row["by"][by] = row["by"].get(by, 0) + 1
        try:   # its hand-off, in its run dir
            if _json.loads(Path(r["dir"], "result.json").read_text()).get("status") == "waiting":
                row["rewaited"] += 1
        except (OSError, TypeError, ValueError, AttributeError):
            pass
    return {"days": days, "per_day": dict(sorted(out.items()))}


def text(s: dict) -> str:
    lines = [f"Task wakes by reason, last {s['days']:g} day(s) (home zone):"]
    for day, reasons in s["per_day"].items():
        total = sum(v["usd"] for v in reasons.values())
        lines.append(f"{day}: {sum(v['wakes'] for v in reasons.values())} wakes, ${total:.2f}")
        for why, v in sorted(reasons.items(), key=lambda kv: -kv[1]["usd"]):
            by = ", ".join(f"{k} {n}" for k, n in sorted(v["by"].items()))
            lines.append(f"  {why}: {v['wakes']} wakes, ${v['usd']:.2f}, {v['rewaited']} handed the wait back ({by})")
    if not s["per_day"]:
        lines.append("  no task wakes recorded")
    return "\n".join(lines)


def wait_kind(probe) -> str:
    s = str(probe or "")
    for kind, pat in (("checks", r"\bchecks\s+--result\b"), ("job", r"\bdetach\s+--check\b"),
                      ("push", r"\bpush\s+--result\b")):
        if re.search(pat, s):
            return kind
    return "probe"


def plan_settings(cfg: dict) -> dict:
    raw = ((cfg or {}).get("waiting") or {}).get("check_plan")
    out = dict(PLAN_DEFAULTS, enabled=raw is not False)
    for k, v in (raw.items() if isinstance(raw, dict) else ()):
        if k == "enabled":
            out[k] = v is not False and str(v).lower() not in ("off", "false", "0", "no")
        elif k in ("tier", "by"):
            out[k] = v if v in (bud.TIER_ORDER if k == "tier" else BYS) else out[k]
        elif k in PLAN_DEFAULTS:
            try:
                out[k] = max(0.0, float(v))
            except (TypeError, ValueError):
                pass
    return out


def record_wait(db: DB, prev: dict, now: float | None = None) -> None:
    """Keep how long a wait of its kind took, from its waiting hand-off to its probe passing."""
    cp = prev.get(PLAN) if isinstance(prev.get(PLAN), dict) else {}
    since, probe = cp.get("since", prev.get("waiting_since")), prev.get("retry_when")
    if not isinstance(since, (int, float)) or not isinstance(probe, str) or not probe.strip():
        return
    got = db.kv(WAITS_KV, {}) or {}
    kind = wait_kind(probe)
    got[kind] = list(got.get(kind) or [])[-(WAITS_KEEP - 1):] + [round(max(0.0, (time.time() if now is None else now) - since))]
    db.set_kv(WAITS_KV, got)


def median_s(db: DB, kind: str) -> tuple[float | None, int]:
    xs = [float(x) for x in ((db.kv(WAITS_KV, {}) or {}).get(kind) or []) if isinstance(x, (int, float))]
    return (statistics.median(xs) if xs else None), len(xs)


def override(raw) -> dict | None:
    """A task_update `check_plan`, kept to its known fields; None or an empty one clears it."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    if isinstance(raw.get("checks"), (int, float)) and not isinstance(raw.get("checks"), bool):
        out["checks"] = max(0, min(12, int(raw["checks"])))
    if isinstance(raw.get("at_s"), list):
        out["at_s"] = sorted(float(x) for x in raw["at_s"][:12] if isinstance(x, (int, float)) and x > 0)
    out.update({k: raw[k] for k in ("tier", "by") if raw.get(k) in (bud.TIER_ORDER if k == "tier" else BYS)})
    return out or None


def plan(db: DB, cfg: dict, result: dict, since: float, over: dict | None = None,
         now: float | None = None) -> dict | None:
    """The check plan of a waiting hand-off whose wait starts at `since` (None: no retry_when, or plans
    are off and the coordinator set none). Checks already past `now` count as done."""
    probe = result.get("retry_when") if isinstance(result, dict) else None
    s, o = plan_settings(cfg), over if isinstance(over, dict) else {}
    if not isinstance(probe, str) or not probe.strip() or not (s["enabled"] or o):
        return None
    kind = wait_kind(probe)
    med, n = median_s(db, kind)
    end = med if med is not None and n >= s["min_samples"] else END_FALLBACK_S[kind]
    first = min(max(end * s["interim_share"], s["interim_min_s"]), s["interim_max_s"])
    if o.get("at_s"):
        offs = list(o["at_s"])
    else:
        offs = [first] + ([end] if end > first else [])
        want = o.get("checks", len(offs))
        offs = offs[:want] + [max(end, first) * (1 + 0.5 * k) for k in range(1, want - len(offs) + 1)]
    at = [round(since + x) for x in offs]
    now = time.time() if now is None else now
    return {"kind": kind, "since": round(since), "at": at, "done": sum(1 for t in at if t <= now),
            "end_s": round(end), "measured": n, "tier": o.get("tier") or s["tier"], "by": o.get("by") or s["by"],
            "src": "coordinator" if o else "default"}


def next_at(prev: dict) -> float | None:
    cp = prev.get(PLAN) if isinstance(prev, dict) else None
    try:
        return float(cp["at"][int(cp["done"])]) if isinstance(cp, dict) and int(cp["done"]) < len(cp["at"]) else None
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def due(prev: dict, now: float) -> dict | None:
    """The planned check due now: {n, of, what (interim, end or late), tier, by, plan (advanced)}."""
    at = next_at(prev)
    if at is None or at > now:
        return None
    cp = prev[PLAN]
    off = at - float(cp.get("since") or at)
    what = "interim" if off < float(cp.get("end_s") or 0) - 1 else "end" if off <= float(cp.get("end_s") or 0) + 1 else "late"
    return {"n": int(cp["done"]) + 1, "of": len(cp["at"]), "what": what, "tier": cp.get("tier"), "by": cp.get("by"),
            "plan": {**cp, "done": int(cp["done"]) + 1}}


def plan_lines(db: DB, cfg: dict, days: float = 1, now: float | None = None) -> list[str]:
    """For the daily review: the defaults, the measured waits per kind and what planned checks found."""
    s, got = plan_settings(cfg), stats(db, "UTC", days, now)["per_day"]
    rows = [v["plan_check"] for v in got.values() if "plan_check" in v]
    wakes, back, usd = (sum(r[k] for r in rows) for k in ("wakes", "rewaited", "usd"))
    out = [f"check plans {'on' if s['enabled'] else 'off'}: interim {s['interim_share']:g} of the expected end "
           f"({s['interim_min_s']:g}-{s['interim_max_s']:g} s), {s['tier']} by {s['by']}; last {days:g} d: "
           f"{wakes} planned-check runs, ${usd:.2f}, {back} only handed the wait back"]
    for kind in END_FALLBACK_S:
        med, n = median_s(db, kind)
        low = "" if n >= s["min_samples"] else f" (fallback {END_FALLBACK_S[kind] / 60:.0f} min below {s['min_samples']:g} samples)"
        out.append(f"{kind} waits: median {f'{med / 60:.0f} min over {n}' if med is not None else 'unmeasured'}{low}")
    return out
