# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider failover: move tasks off a provider that cannot start new work for a long while.

A queued (deferred included) or waiting task whose provider (its own pin, else the core provider)
cannot start new work moves, model-free, to another provider that can, when that provider's stop
lasts more than coordinator.provider_failover_after_h hours (default AFTER_H; 0 turns failover off).
A provider cannot start new work when its auth breaker is open (logged out: no known end), its
limit pause runs, or its budget gate is red (a plan window at the line, a dollar cap, the runaway
guard). Its end is the pause's end, or for a red gate the reset of every window or cap holding it;
a red gate whose end is unknown moves nothing.

A pin to a provider other than the core one is a preference unless it is marked required (the
task's `pin_required`, kept as the label REQUIRED_LABEL): such a task moves at once, with no wait,
whenever its provider cannot start new work, whatever the reason and its end (hold()), including a
gate below red that allows no new start (plan pacing just under the line). Only the
user's own words mark a pin required; an unmarked pin, old tasks' included, is a preference.

Targets: coordinator.provider_failover_to (provider names, in order of preference) and then the
core provider; a preference pin goes to the core provider first. A target must be configured and
installed, logged in, not paused, with a gate that is not red and allows new work. The move clears
the task's model pin (one provider's names), keeps its effort where the new provider takes it (else
the nearest it takes), sets the tier that matches the effort (e.g. Codex xhigh to deep), keeps its
budget, and adds a spec note saying what moved, why and when the old provider resets. Running tasks
are never touched: each is reported once. A task (or one it continues or reviews) marked required
stays put. Every move is an event in the feed (kind KIND), listed in the daily review, and one low
alert. coordinator.provider_failover_after_h 0 turns every move off.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from . import headline, timefmt
from .db import DB

KIND = "provider_failover"
REQUIRED_LABEL = "pin:required"      # the user's own words require the task's provider or model
PREFERRED_LABEL = "pin:preferred"    # pin_required set false: overrides a mark on a task it continues
NOTE_PREFIX = "[provider failover"
AFTER_H = 4.0
REPORTED_KEY = "failover_reported"     # kv: {"<task>:<provider>": ts} running tasks already reported
CHAIN_RE = re.compile(r"^(?:continues|auto_review|review_fix):(\d+)$")
EFFORT_RANK = ("minimal", "low", "medium", "high", "xhigh", "max")
TIERS = ("light", "standard", "deep")


def after_h(cfg: dict) -> float:
    try:
        v = (cfg.get("coordinator") or {}).get("provider_failover_after_h", AFTER_H)
        return max(0.0, float(AFTER_H if v is None else v))
    except (TypeError, ValueError):
        return AFTER_H


def after_h_of(v: Any) -> float:
    """coordinator.provider_failover_after_h as config_set takes it: hours >= 0 (0 turns failover off)."""
    try:
        h = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"provider_failover_after_h needs a number of hours >= 0 (0: off), not {v!r}") from None
    if not h >= 0:
        raise ValueError(f"provider_failover_after_h needs a number of hours >= 0 (0: off), not {v!r}")
    return h


def to_of(v: Any, providers: dict | None) -> list[str]:
    """coordinator.provider_failover_to as config_set takes it: configured provider names, best first
    ("a,b" or a list; "" or [] leaves only the core provider)."""
    if v is None or v == "":
        return []
    names = v.split(",") if isinstance(v, str) else v if isinstance(v, (list, tuple)) else None
    if names is None or not all(isinstance(n, str) for n in names):
        raise ValueError(f"provider_failover_to needs a list of provider names, not {v!r}")
    out = [n.strip() for n in names if n.strip()]
    if bad := [n for n in out if n not in (providers or {})]:
        raise ValueError(f"provider_failover_to: not a configured provider: {', '.join(bad)} "
                         f"(configured: {', '.join(sorted(providers or {})) or 'none'})")
    return [n for i, n in enumerate(out) if n not in out[:i]]


def targets(cfg: dict) -> list[str]:
    """Providers a task may move to, best first: coordinator.provider_failover_to, then the core one."""
    raw = (cfg.get("coordinator") or {}).get("provider_failover_to") or []
    names = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else []
    core = cfg.get("core_provider") or "claude"
    out = [n for n in (str(n).strip() for n in names) if n in (cfg.get("providers") or {})] + [core]
    return [n for i, n in enumerate(out) if n not in out[:i]]


def stop(gate: Any, pause: dict | None, logged_out: bool, now: float) -> tuple[str, float | None] | None:
    """Why the provider cannot start new work and when that ends (None: logged out, no known end), or
    None if it can or its red gate has no known end. `gate` is a budget Gate (or its dict), `pause`
    its limited:<provider> record."""
    got = _held(gate, pause, logged_out, now)
    return None if got is None or (got[1] is None and not logged_out) else got


def hold(gate: Any, pause: dict | None, logged_out: bool, now: float) -> tuple[str, float | None] | None:
    """Like stop(), but a red gate whose end is unknown holds too (end None), and so does a gate
    below red that allows no new start (plan pacing's 'no new starts': max_parallel 0), ending at
    the reset of the plan windows that leave no room: what moves a preference pin at once."""
    if got := _held(gate, pause, logged_out, now):
        return got
    g = gate.as_dict() if hasattr(gate, "as_dict") else (gate or {})
    if not g or (g.get("allow_new_work", True) and (g.get("max_parallel") is None or g["max_parallel"] > 0)):
        return None
    full = [w.get("resets_at") for w in (g.get("numbers") or {}).get("plan") or [] if w.get("allowed") == 0]
    end = max(full) if full and all(full) else None
    return "; ".join(g.get("reasons") or []) or "budget gate allows no new start", end


def _held(gate: Any, pause: dict | None, logged_out: bool, now: float) -> tuple[str, float | None] | None:
    """Logged out, a running limit pause or a red gate (end None where unknown)."""
    if logged_out:
        return "logged out", None
    if pause and float(pause.get("until") or 0) > now:
        return f"provider limit: {pause.get('note') or 'paused'}", float(pause["until"])
    g = gate.as_dict() if hasattr(gate, "as_dict") else (gate or {})
    if not g or g.get("allow_new_work", True) or g.get("level") != "red":
        return None
    reasons, n = list(g.get("reasons") or []), g.get("numbers") or {}
    ends: list[float | None] = []
    for r in reasons:
        if " window at " in r and "the project stops at" in r:
            rows = [w for w in n.get("plan") or [] if w.get("allowed") == 0 and r.startswith(f"{w.get('window')} ")]
            ends += [w.get("resets_at") for w in rows] or [None]
        elif r.startswith("global daily cap"):
            ends.append(n.get("global_resets_at"))
        elif r.startswith("cap reached"):
            ends.append(n.get("day_end"))   # a rolling day or week has no fixed end
        elif r.startswith("runaway guard"):
            ends.append(now + 3600)
        elif r.startswith("provider limit"):
            ends.append(float(pause["until"]) if pause and pause.get("until") else None)
    if not ends or any(e is None for e in ends):
        return "; ".join(reasons) or "budget gate red", None   # no known end
    return "; ".join(reasons), max(float(e) for e in ends)


def _labels(task: dict) -> list:
    try:
        v = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def required(db: DB, task: dict, depth: int = 8) -> bool:
    """Whether the task's pin is marked required: its own REQUIRED_LABEL, or one on a task it
    continues, reviews or fixes (so a review or fix keeps the pin its origin requires), unless its
    own PREFERRED_LABEL says the pin is only a preference."""
    seen, todo = set(), [task]
    while todo and len(seen) <= depth:
        t = todo.pop()
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        labels = _labels(t)
        if PREFERRED_LABEL in labels:
            if t is task:
                return False
            continue   # an origin marked a preference does not pass a mark further up on
        for lb in labels:
            if lb == REQUIRED_LABEL:
                return True
            m = CHAIN_RE.match(lb) if isinstance(lb, str) else None
            if m and (up := db.task(int(m.group(1)))):
                todo.append(up)
    return False


def mapped_tier(cfg: dict, provider: str, task: dict) -> str:
    """The tier that matches the task's effort pin on `provider` (its tier when it pins none)."""
    eff = (task.get("effort") or "").strip().lower()
    if not eff:
        return task.get("tier") or "standard"
    tiers = ((cfg.get("providers") or {}).get(provider) or {}).get("tiers") or {}
    for name in reversed(TIERS):
        if str((tiers.get(name) or {}).get("effort") or "").lower() == eff:
            return name
    rank = EFFORT_RANK.index(eff) if eff in EFFORT_RANK else 3
    return "light" if rank <= 1 else "standard" if rank <= 3 else "deep"


def kept_effort(provider: str, effort: str | None) -> str | None:
    """The task's effort pin as `provider` takes it: the same level, else the nearest one it takes
    (None when it pins none or the provider takes no effort levels)."""
    eff = (effort or "").strip().lower()
    if not eff:
        return None
    try:
        from .providers import get_provider
        takes = tuple(get_provider(provider).efforts)
    except Exception:
        takes = ()
    if eff in takes:
        return eff
    rank = EFFORT_RANK.index(eff) if eff in EFFORT_RANK else 3
    ranked = [e for e in takes if e in EFFORT_RANK]
    return min(ranked, key=lambda e: abs(EFFORT_RANK.index(e) - rank)) if ranked else None


def note(move: dict, where) -> str:
    """What moved, why and until when, in plain words (the spec note and feed line say this)."""
    t, old = move["task"], move["old"]
    pin = ", ".join(f"{k} {t[k]}" for k in ("model", "effort") if t.get(k))
    until = f"until {timefmt.long(move['ends'], where)}" if move["ends"] else "with no known end"
    kept = f", effort {move['effort']}" if move.get("effort") else ""
    if move.get("at_once"):
        return (f"moved at once from {old}" + (f" ({pin})" if pin else "") + f" to {move['new']}, tier "
                f"{move['tier']}{kept}: {old} cannot start new work ({move['why']}) {until}, and its pin is a "
                f"preference, not one the user's own words require (pin_required).")
    return (f"moved from {old}" + (f" ({pin})" if pin else "") + f" to {move['new']}, tier {move['tier']}{kept}: "
            f"{old} cannot start new work ({move['why']}) {until}. Its pin was not marked required "
            f"(pin_required).")


NOTE_RE = re.compile(r"(?:\n\n|^)" + re.escape(NOTE_PREFIX) + r"[^\n]*")


def strip_notes(spec: str) -> str:
    """The spec as it was before its failover notes (a move is no change of the task's own spec)."""
    return NOTE_RE.sub("", spec or "") if NOTE_PREFIX in (spec or "") else spec or ""


def plan(db: DB, cfg: dict, stops: dict[str, tuple[str, float | None]], ok: list[str],
         now: float, holds: dict[str, tuple[str, float | None]] | None = None) -> tuple[list[dict], list[dict]]:
    """The moves to make now and the running tasks to report: ([{task, old, new, tier, effort, why,
    ends, at_once}], [{task, provider, why, ends}]). `stops` maps each provider that cannot start new
    work to (why, end); `holds` (stops when not given) adds those whose end is unknown, which move a
    preference pin at once; `ok` lists the targets that can, best first."""
    holds = stops if holds is None else holds
    limit = after_h(cfg)
    if not limit or not holds:
        return [], []
    core = cfg.get("core_provider") or "claude"
    moves, running = [], []
    for t in db.q("SELECT * FROM tasks WHERE status IN ('queued','waiting','running') ORDER BY priority, id"):
        old = t["provider"] or core
        if old not in holds:
            continue
        # A pin to another provider than the core one, not marked required, is a preference: it
        # waits for nothing. Anything else moves only off a stop longer than the threshold.
        at_once = old != core and t["status"] != "running" and not required(db, t)
        if not at_once:
            if old not in stops:
                continue
            why, ends = stops[old]
            if ends is not None and ends - now <= limit * 3600:
                continue
        else:
            why, ends = holds[old]
        if t["status"] == "running":
            running.append({"task": t, "provider": old, "why": why, "ends": ends})
            continue
        order = ([core] if at_once and core in ok else []) + ok
        new = next((p for p in order if p != old), None)
        if not new or (not at_once and required(db, t)):
            continue
        moves.append({"task": t, "old": old, "new": new, "tier": mapped_tier(cfg, old, t),
                      "effort": kept_effort(new, t.get("effort")), "why": why, "ends": ends, "at_once": at_once})
    return moves, running


def apply(db: DB, cfg: dict, move: dict, now: float, where) -> str:
    """Re-route one task as `move` says; returns the feed line."""
    t, body = move["task"], " ".join(note(move, where).split())
    text = f"{NOTE_PREFIX} {timefmt.long(now, where)}] {body[0].upper()}{body[1:]}"
    line = headline.join(t["title"], body, 80, f"#{t['id']} ")
    with db.tx():
        db.update_task(t["id"], provider=move["new"], model=None, effort=move.get("effort"), tier=move["tier"],
                       **({"tier_source": "failover"} if move["tier"] != t.get("tier") else {}),
                       spec=f"{t['spec']}\n\n{text}" if t["spec"] else text)
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", KIND, "low", line[:500], "handled", t["id"]))
    return line


def report_running(db: DB, items: list[dict], now: float, where) -> list[str]:
    """Feed lines for running tasks on a stopped provider, each once per task and provider."""
    seen = db.kv(REPORTED_KEY, {}) or {}
    out = []
    for r in items:
        key = f"{r['task']['id']}:{r['provider']}"
        if key in seen:
            continue
        until = f"until {timefmt.long(r['ends'], where)}" if r["ends"] else "with no known end"
        line = headline.join(r["task"]["title"], f"left running on {r['provider']}, which cannot "
                             f"start new work ({r['why']}) {until}; a later run of it moves unless its pin is required",
                             80, f"#{r['task']['id']} ")
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", KIND, "low", line[:500], "handled", r["task"]["id"]))
        seen[key] = now
        out.append(line)
    if out:
        db.set_kv(REPORTED_KEY, {k: v for k, v in seen.items() if now - float(v) < 14 * 86400})
    return out


def review_lines(db: DB, now: float | None = None, hours: float = 24, where=None) -> list[str]:
    """For the daily review: every failover event of the last `hours`. A logged-out cause whose auth
    breaker has closed since says when it ended and how long it lasted, with the task's status now,
    instead of 'no known end' (that stays only while the outage is open)."""
    from . import alerts
    now = time.time() if now is None else now
    rows = db.q("SELECT ts,text,task FROM events WHERE kind=? AND ts>=? ORDER BY ts", (KIND, now - hours * 3600))
    out = []
    for r in rows:
        text = r["text"]
        m = re.search(r"(?:left running on|moved (?:at once )?from) (\S+?)[,( ]", text)
        if m and "(logged out) with no known end" in text:
            rec = db.kv(alerts.BREAKER + m.group(1)) or {}
            closed = rec.get("closed")
            if not rec.get("open", True) and closed and float(closed) >= float(r["ts"]):
                start = float(rec.get("opened") or r["ts"])
                ended = (f"(logged out) (ended {timefmt.long(float(closed), where)}, lasted "
                         f"{max(0.0, float(closed) - start) / 3600:.1f} h)")
                text = text.replace("(logged out) with no known end", ended)
                t = db.task(r["task"]) if r["task"] else None
                if t:
                    text += f"; #{t['id']} is now {t['status']}"
        out.append(text)
    return out
