# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp stats --coord-session`: how the persistent coordinator session (coordsession.py) did, per day
in the project's home zone, against the stateless coordinator it replaced.

A session wake is a coordinator run whose note has turn.session (coordinator.session.mode on): mode
"resume", "new" (a new session) or "stateless" (paused, spend guard, rerun). Per day: wakes, $/wake,
context growth per resumed wake (its first call's prompt less the previous wake's of the same
session), new sessions, in-place compactions (session id, tokens before and after, from
coordsession's `compactions`), tool calls per wake (tool_use blocks in its transcript; any provider), and the cache-read
share of its prompt tokens. The baseline is every coordinator run of the 7 days before the first
"resume" or "new" wake: turns/day, $/turn and $/day. A day breaks even at <= 2 tool calls and
<= 20k context growth per wake; it is over the baseline when all its coordinator runs cost more
than the baseline's $/day (today: so far).

Why each session wake did or did not resume (reason(), from the mode and why the daemon recorded at
its decision, coordsession.decide): resumed, compacted in place (older releases: handover, a rollover
at its context size), a new session
for another cause, or a stateless fallback (spend guard, a failed resume with its error, paused after
failed resumes). The fallback rate is stateless wakes over session wakes, so only time with the mode
on counts. Coordinator wakes with the mode off are counted apart: the daemon records turn.session_off;
an older wake without either record ran with the mode off or an agent that cannot resume (the daemon
recorded turn.session for every other one)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import timefmt

BREAK_EVEN_TOOL_CALLS = 2
BREAK_EVEN_GROWTH = 20000
BASELINE_DAYS = 7


def _note(raw) -> dict:
    try:
        got = json.loads(raw or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def _part(r, key: str) -> dict:
    got = _note(r["note"]).get(key)
    return got if isinstance(got, dict) else {}


def session_of(r) -> dict | None:
    s = _part(r, "turn").get("session")
    return s if isinstance(s, dict) else None


def tool_calls(r, runs_dir: Path) -> int | None:
    """tool_use blocks in the run's transcript, each once (a stream repeats a message per block);
    another agent's stream (Codex): its own tool calls (Provider.activity). None without a transcript."""
    out = (Path(r["dir"]) if r["dir"] else Path(runs_dir) / str(r["id"])) / "output.jsonl"
    try:
        lines = out.read_text(errors="replace").splitlines()
    except OSError:
        return None
    prov = r["provider"] if "provider" in r.keys() else "claude"
    if prov not in ("claude", "chat"):   # chat is Claude Code on a served model: the same stream
        from .providers import get_provider
        try:
            calls = get_provider(prov).activity(out)[0]
        except Exception:
            return None
        return len({c.get("id") or i for i, c in enumerate(calls) if c.get("kind") == "call"})
    ids, bare = set(), 0
    for line in lines:
        try:
            msg = (json.loads(line) or {}).get("message")
        except (ValueError, AttributeError):
            continue
        for b in msg.get("content") or [] if isinstance(msg, dict) and isinstance(msg.get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                if b.get("id"):
                    ids.add(b["id"])
                else:
                    bare += 1
    return len(ids) + bare


NEW_WHY = {"no session": "new: no session", "prefix changed": "new: prompt, charter or schema changed",
           "model or effort changed": "new: model or effort changed", "last run not ok": "new: last wake failed",
           "session not saved": "new: transcript gone"}


def _provider_gaps() -> set:
    """coordsession.gap() of each provider that keeps no coordinator session (its stateless why)."""
    from . import coordsession
    from .providers import all_providers
    return {g for g in map(coordsession.gap, all_providers()) if g}


def reason(s: dict, status: str = "ok") -> str:
    """The reason label of one session wake (its turn.session) for the per-day counts."""
    mode, why = s.get("mode"), str(s.get("why") or "")
    if mode == "resume":
        if status not in ("ok", "running"):
            return "resume failed"
        # "handover" and "context reached" (below) are older releases' rollovers, kept for their days.
        return "handover" if why == "handover" else "compacted in place" if why == "compact" else "resumed"
    if mode == "new":
        return "rollover" if why.startswith("context reached") else NEW_WHY.get(why, "new: other")
    if why == "spend guard tripped":
        return "spend guard"
    if why.startswith("rerun"):
        return "failed resume (rerun)"
    if why.startswith("paused"):
        return "paused after failed resumes"
    if why in _provider_gaps():
        return "provider keeps no session"
    return "decide failed" if why.startswith("decide failed") else "stateless: other"


def _rollover_tokens(s: dict) -> int | None:
    words = str(s.get("why") or "").split()
    return int(words[2]) if s.get("mode") == "new" and words[:2] == ["context", "reached"] and words[2:3] \
        and words[2].isdigit() else None


def run_error(r, runs_dir: Path) -> str:
    """A failed wake's error from its transcript's result event (e.g. error_max_budget_usd), else ""."""
    out = (Path(r["dir"]) if r["dir"] else Path(runs_dir) / str(r["id"])) / "output.jsonl"
    try:
        lines = out.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            errs = ev.get("errors") if isinstance(ev.get("errors"), list) else []
            return "; ".join(str(x) for x in [ev.get("subtype") if ev.get("is_error") else None, *errs] if x)[:200]
    return ""


def _first_context(r) -> int | None:
    v = _part(r, "cache_telemetry").get("first_context")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _mean(xs: list) -> float | None:
    return round(sum(xs) / len(xs), 1) if xs else None


def report(db, runs_dir, tz: str, days: float = 7, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    cols = "id, started, status, cost_usd, input_tokens, cache_read_tokens, cache_write_tokens, note, session_id, dir, provider"
    noted = [r for r in db.q(f"SELECT {cols} FROM runs WHERE role='coordinator' AND note LIKE '%session%' "
                             "ORDER BY started, id") if session_of(r)]
    first = next((r["started"] for r in noted if session_of(r).get("mode") in ("resume", "new")), None)
    end = first if first is not None else now
    base = db.q("SELECT started, cost_usd FROM runs WHERE role='coordinator' AND started>=? AND started<?",
                (end - BASELINE_DAYS * 86400, end))
    # A project younger than the window: its own span, at least a day (one early turn is no rate).
    span = max(1.0, (end - min(r["started"] for r in base)) / 86400) if base else 0.0
    usd = sum(r["cost_usd"] or 0 for r in base)
    baseline = {"since": end - BASELINE_DAYS * 86400, "until": end, "turns": len(base), "days": round(span, 2),
                "turns_per_day": round(len(base) / span, 1) if span > 0 else None,
                "usd_per_turn": round(usd / len(base), 3) if base else None,
                "usd_per_day": round(usd / span, 2) if span > 0 else None}
    since = now - days * 86400
    prev, by_day, off = {}, {}, {}
    for r in db.q("SELECT started, note FROM runs WHERE role='coordinator' AND started>=?", (since,)):
        if not session_of(r):   # mode off (or an agent that cannot resume), recorded or not
            why = str(_part(r, "turn").get("session_off") or "off, not recorded")
            day = off.setdefault(timefmt.today(r["started"], tz), {})
            day[why] = day.get(why, 0) + 1
    for r in noted:   # the previous wake of each session, also from before the window
        s, sid = session_of(r), r["session_id"] or ""
        last, fc = prev.get(sid), _first_context(r)
        if r["started"] >= since:
            d = by_day.setdefault(timefmt.today(r["started"], tz), {"runs": [], "growth": []})
            d["runs"].append((r, s))
            if s.get("mode") == "resume" and sid and fc is not None and last is not None:
                d["growth"].append(fc - last)
        if sid and s.get("mode") in ("resume", "new"):
            prev[sid] = fc
    from . import coordsession
    kept = db.kv(coordsession.KEY)
    kept = kept.get("compactions") if isinstance(kept, dict) else None
    compactions: dict = {}   # coordsession.record's rows in the window, by day
    for c in kept if isinstance(kept, list) else []:
        if isinstance(c, dict) and isinstance(c.get("at"), (int, float)) and c["at"] >= since:
            compactions.setdefault(timefmt.today(c["at"], tz), []).append(
                {k: c.get(k) for k in ("at", "session", "before", "after", "how")})
    out = []
    today = timefmt.today(now, tz)
    for day, d in sorted(by_day.items()):
        rs = [r for r, _ in d["runs"]]
        modes = [s.get("mode") for _, s in d["runs"]]
        calls = [c for c in (tool_calls(r, runs_dir) for r in rs) if c is not None]
        cost = sum(r["cost_usd"] or 0 for r in rs)
        read = sum(r["cache_read_tokens"] or 0 for r in rs)
        prompt = read + sum((r["input_tokens"] or 0) + (r["cache_write_tokens"] or 0) for r in rs)
        day_usd = db.one("SELECT COALESCE(SUM(cost_usd), 0) u FROM runs WHERE role='coordinator' AND started>=? "
                         "AND started<?", _day_bounds(day, tz))["u"]
        growth, tc = _mean(d["growth"]), _mean(calls)
        reasons, errors = {}, []
        for r, s in d["runs"]:
            why = reason(s, r["status"])
            reasons[why] = reasons.get(why, 0) + 1
            if r["status"] not in ("ok", "running"):   # the rerun's note also names a failed resume's error
                errors.append(f"run {r['id']} ({s.get('mode')}, {r['status']}): "
                              f"{run_error(r, runs_dir) or 'no error recorded'}"[:240])
        sizes = [t for t in (_rollover_tokens(s) for _, s in d["runs"]) if t is not None]
        out.append({"day": day, "partial": day == today, "wakes": len(rs), "resumed": modes.count("resume"),
                    "rollovers": modes.count("new"), "stateless": modes.count("stateless"),
                    "usd": round(cost, 2), "usd_per_wake": round(cost / len(rs), 3),
                    "reasons": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
                    "fallback_rate": round(modes.count("stateless") / len(rs), 3),
                    "rollover_tokens": round(sum(sizes) / len(sizes)) if sizes else None,
                    "failed_wakes": errors[:5], "mode_off": off.get(day, {}),
                    "compactions": compactions.get(day, []),
                    "growth_per_wake": growth, "growth_wakes": len(d["growth"]),
                    "tool_calls_per_wake": tc, "tool_call_wakes": len(calls),
                    "cache_read_ratio": round(read / prompt, 3) if prompt else None,
                    "coordinator_usd": round(day_usd, 2),
                    "over_baseline": None if baseline["usd_per_day"] is None else day_usd > baseline["usd_per_day"],
                    "break_even": None if growth is None or tc is None else
                    tc <= BREAK_EVEN_TOOL_CALLS and growth <= BREAK_EVEN_GROWTH})
    return {"zone": tz, "days": days, "first_session_wake": first, "baseline": baseline, "by_day": out}


def _day_bounds(day: str, tz: str) -> tuple[float, float]:
    from datetime import datetime, timedelta
    start = datetime.fromisoformat(day).replace(tzinfo=timefmt.tzinfo(tz))
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def text(s: dict, name: str = "") -> str:
    b = s["baseline"]
    f = lambda v, fmt: "n/a" if v is None else format(v, fmt)
    lines = [f"Coordinator session{' of ' + name if name else ''}, last {s['days']:g} days ({s['zone']}). "
             f"Stateless baseline, {b['turns']} turns in the {BASELINE_DAYS} days before "
             + ("the first session wake" if s["first_session_wake"] else "now (no session wake yet)")
             + f": {f(b['turns_per_day'], '.1f')} turns/day, ${f(b['usd_per_turn'], '.3f')}/turn, "
             f"${f(b['usd_per_day'], '.2f')}/day"]
    if not s["by_day"]:
        lines.append("  no session-mode wakes in the window (coordinator.session.mode on)")
    for d in s["by_day"]:
        g = d["growth_per_wake"]
        even = {None: "unknown", True: "met", False: "missed"}[d["break_even"]]
        over = {None: "no baseline", True: "OVER baseline", False: "under baseline"}[d["over_baseline"]]
        lines.append(
            f"  {d['day']}{' (so far)' if d['partial'] else ''}: {d['wakes']} wakes ({d['resumed']} resumed, "
            f"{d['rollovers']} new, {d['stateless']} stateless), ${d['usd_per_wake']:.3f}/wake, "
            f"context growth {'n/a' if g is None else f'{g / 1000:+.1f}k'}/wake, {len(d.get('compactions') or [])} "
            f"compactions in place, "
            f"{f(d['tool_calls_per_wake'], '.1f')} tool calls/wake, cache reads "
            f"{'n/a' if d['cache_read_ratio'] is None else format(d['cache_read_ratio'], '.0%')}; "
            f"break-even (<= {BREAK_EVEN_TOOL_CALLS} tool calls, <= {BREAK_EVEN_GROWTH // 1000}k growth/wake): "
            f"{even}; coordinator ${d['coordinator_usd']:.2f} vs ${f(b['usd_per_day'], '.2f')}/day: {over}")
        size = d["rollover_tokens"]
        why = ", ".join(f"{k} {v}" + (f" (mean {size / 1000:.0f}k tokens)" if k == "rollover" and size else "")
                        for k, v in d["reasons"].items())
        offs = sum(d["mode_off"].values())
        lines.append(f"    why: {why}; fallback to stateless {d['fallback_rate']:.0%} of {d['wakes']} wakes with the "
                     f"mode on" + (f"; {offs} more with it off ("
                                   + ", ".join(f"{k} {v}" for k, v in sorted(d["mode_off"].items())) + ")"
                                   if offs else ""))
        lines.extend(f"    failed wake: {e}" for e in d["failed_wakes"])
        lines.extend(f"    compacted in place: session {str(c['session'] or '-')[:8]} at {timefmt.short(c['at'], s['zone'])}, "
                     f"{int(c['before'] or 0) / 1000:.0f}k -> {int(c['after'] or 0) / 1000:.0f}k tokens ({c['how']})"
                     for c in d.get("compactions") or [])
    return "\n".join(lines)
