# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The cost levers, each behind its own release-ring flag (rollout.FLAGS), all off by default:

- rings.coord_wake_settle (key coordinator.wake_settle): wakesettle.py settles routine coordinator wakes
  without a model (a probe-passed spec edit that only records the pass is one of them); its decision counter
  (wake_decisions) records every pre-screen gate's verdict always. The daemon also runs the waste ledger
  once a day without a model (Daemon.check_daily_waste), and after a coordinator wake of TOOLS_STEER or
  more tool calls the next digest carries a steer to batch reads into one call or hand reading to a worker.
- rings.eta_history (key eta.history), shadow: etaledger.record also stores the ETA from eta.history next to
  the shown one; nothing users see changes; eta_shadow_line gives the baseline.

Precedence (rollout.on): a project's own explicit value for the flag's key wins, else the ring rule, else off.

Metrics (status_lines in `ttp status`, audit_lines in the daily review), last 7 days:
- wakes: wakesettle.line; wake tools: coordinator wakes with TOOLS_STEER or more tool calls and their $.
- eta: the shadow baseline (ETAs compared, median and p90 absolute error of each method), flag on only.
"""
from __future__ import annotations

import json
import time
from typing import Any


STATUS_KEY = "cost_levers_status"   # kv: {"at", "lines"}, the status lines cached
STATUS_TTL_S = 1800.0
WINDOW_S = 7 * 86400.0
SAMPLE = 200                 # coordinator runs whose transcript is read for tool calls
TOOLS_STEER = 8              # a coordinator wake with this many tool calls steers the next one
STEER_KEY = "wake_tools_steer"      # kv: {"run", "tools", "at"} of the wake that made TOOLS_STEER+ calls
STEER_LINE = ("Last wake made {tools} tool calls (run {run}): each call re-reads the whole context. Batch "
              "independent read-only lookups into one shell call (`a; b; c`), and hand reading code or logs "
              "beyond a few short excerpts to a worker or subagent.")


def _dig(d: Any, key: str) -> Any:
    for part in key.split("."):
        d = d.get(part) if isinstance(d, dict) else None
    return d


def lever_on(p, flag: str) -> bool:
    """Whether lever `flag` is on for project `p`: its own explicit value of the flag's key, else the ring rule.
    A broken ring or config file never turns a lever on."""
    from . import rollout
    try:
        raw = p.raw_config()
        key = rollout.FLAGS[flag]["key"]
        if rollout.own_setting(raw, key):
            return bool(_dig(raw, key))
        return rollout.on(flag, p.name, raw=raw)
    except Exception:
        return False


def _note(raw: Any) -> dict:
    try:
        got = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def wake_tools_report(p, since: float, until: float | None = None) -> dict:
    """{wakes, heavy, usd, heavy_usd, max}: coordinator runs with a transcript in the window, those with
    TOOLS_STEER or more tool calls (newest SAMPLE read) and their $."""
    from . import coordstats
    until = time.time() if until is None else until
    rows = p.db.q("SELECT id, dir, provider, cost_usd FROM runs WHERE role='coordinator' AND started>=? "
                  "AND started<? AND status!='running' ORDER BY started DESC LIMIT ?", (since, until, SAMPLE))
    n = heavy = top = 0
    usd = heavy_usd = 0.0
    for r in rows:
        c = coordstats.tool_calls(r, p.runs)
        if c is None:
            continue
        n += 1
        usd += float(r["cost_usd"] or 0)
        top = max(top, c)
        if c >= TOOLS_STEER:
            heavy += 1
            heavy_usd += float(r["cost_usd"] or 0)
    return {"wakes": n, "heavy": heavy, "usd": round(usd, 2), "heavy_usd": round(heavy_usd, 2), "max": top}


def wake_tools_line(p, since: float, until: float | None = None) -> str:
    r = wake_tools_report(p, since, until)
    if not r["wakes"]:
        return ""
    flag = "on" if lever_on(p, "coord_wake_settle") else "off"
    return (f"wake tools [{flag}]: {r['heavy']} of {r['wakes']} coordinator wakes made {TOOLS_STEER}+ tool "
            f"calls (${r['heavy_usd']:.2f} of ${r['usd']:.2f}; most {r['max']})")


def note_wake_tools(p, run_id: int, tools: int | None, now: float) -> None:
    """After a coordinator wake: with the lever on, a wake of TOOLS_STEER+ tool calls sets the steer for the
    next digest; a lighter one clears it. Lever off: nothing is read or written."""
    if tools is None or not lever_on(p, "coord_wake_settle"):
        return
    if tools >= TOOLS_STEER:
        p.db.set_kv(STEER_KEY, {"run": int(run_id), "tools": int(tools), "at": now})
    elif p.db.kv(STEER_KEY, None):
        p.db.set_kv(STEER_KEY, None)


def steer_lines(p) -> list[str]:
    """The next digest's steer after a heavy wake (see note_wake_tools); empty with the lever off."""
    st = p.db.kv(STEER_KEY, None)
    if not isinstance(st, dict) or not lever_on(p, "coord_wake_settle"):
        return []
    return ["## Tool calls: " + STEER_LINE.format(tools=st.get("tools"), run=st.get("run"))]


def eta_shadow_line(p, since: float, until: float | None = None) -> str:
    """The eta_history shadow baseline; empty with the flag off."""
    from . import etaledger
    if not lever_on(p, "eta_history"):
        return ""
    return etaledger.shadow_line(etaledger.shadow(p.db, since))


# --- status and daily review -------------------------------------------------------------------------

def lines(p, since: float, until: float | None = None) -> list[str]:
    from . import wakesettle
    out = []
    for fn in (lambda p, s, u: wakesettle.line(p.db, s, u), wake_tools_line, eta_shadow_line):
        try:
            if ln := fn(p, since, until):
                out.append(ln)
        except Exception as e:   # one broken metric never hides the others
            out.append(f"cost lever metric failed: {type(e).__name__}: {e}"[:200])
    return out


def status_lines(p, now: float | None = None) -> list[str]:
    """The levers' metrics over the last 7 days for `ttp status`, cached STATUS_TTL_S."""
    now = time.time() if now is None else now
    seen = p.db.kv(STATUS_KEY, {}) or {}
    if now - float(seen.get("at") or 0) < STATUS_TTL_S and isinstance(seen.get("lines"), list):
        got = seen["lines"]
    else:
        got = lines(p, now - WINDOW_S, now)
        try:
            p.db.set_kv(STATUS_KEY, {"at": now, "lines": got})
        except Exception:
            pass
    return [f"cost levers (7d): {x}" for x in got]


def audit_lines(p, since: float) -> list[str]:
    """The daily review's lever lines: the wake-tools and eta shadow metrics (the wake line has its own
    audit line)."""
    out = []
    for fn in (wake_tools_line, eta_shadow_line):
        try:
            if ln := fn(p, since):
                out.append(ln)
        except Exception as e:
            out.append(f"cost lever metric failed: {type(e).__name__}: {e}"[:200])
    return out
