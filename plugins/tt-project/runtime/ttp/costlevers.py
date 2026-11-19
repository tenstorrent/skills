# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The cost levers, each behind its own release-ring flag (rollout.FLAGS), all off by default:

- rings.fresh_continue (key worker.fresh_continue): a continue, fix or retry run starts fresh instead of
  resuming an earlier worker session (ctxfork.prepare skips the resume source), with a compact hand-off
  (handoff_block: the earlier hand-offs, the review findings it fixes, the diff). A run that forks the
  coordinator keeps the fork (full context stays). Its run note's coord_context carries "fresh": true.
- rings.coord_wake_settle (key coordinator.wake_settle): wakesettle.py settles routine coordinator wakes
  without a model (a probe-passed spec edit that only records the pass is one of them); its decision counter
  (wake_decisions) records every pre-screen gate's verdict always. The daemon also runs the waste ledger
  once a day without a model (Daemon.check_daily_waste), and after a coordinator wake of TOOLS_STEER or
  more tool calls the next digest carries a steer to batch reads into one call or hand reading to a worker.
- rings.eta_history (key eta.history), shadow: etaledger.record also stores the ETA from eta.history next to
  the shown one; nothing users see changes; eta_shadow_line gives the baseline.
- rings.output_trim (key output.trim): workers are asked for short hand-offs and reports (no output cap),
  and an unpinned work or harness task whose spec is mechanical runs at medium effort (effort_pick). Code
  tasks keep effortab's A/B and Jev's pick; reviews and graders are never picked here, and a conclusion
  run's effort floor still lifts it.

Precedence (rollout.on): a project's own explicit value for the flag's key wins, else the ring rule, else off.

Metrics (status_lines in `ttp status`, audit_lines in the daily review), last 7 days:
- fresh: runs per arm (fresh vs resumed), $ per landing and landed rate of the arm's code tasks, tool calls
  per run, review rounds per task; landings judged by landed.recorded (push queue, task_landings,
  supersessions and the tasks that carry a change on), shown against task_landings alone.
- wakes: wakesettle.line; wake tools: coordinator wakes with TOOLS_STEER or more tool calls and their $.
- eta: the shadow baseline (ETAs compared, median and p90 absolute error of each method), flag on only.
- output: output tokens and $ per worker run by kind, trimmed-effort runs vs the rest.
"""
from __future__ import annotations

import json
import re
import statistics
import subprocess
import time
from typing import Any

from .util import task_labels

TRIM_KEY = "output_trim"     # run note: {"effort", "from", "why"} of an effort pick
STATUS_KEY = "cost_levers_status"   # kv: {"at", "lines"}, the status lines cached
STATUS_TTL_S = 1800.0
WINDOW_S = 7 * 86400.0
HANDOFF_CHARS = 48000        # about 12k tokens
RESULT_CHARS = 8000
DIFF_CHARS = 24000
SAMPLE = 200                 # runs per arm whose transcript is read for tool calls
TOOLS_STEER = 8              # a coordinator wake with this many tool calls steers the next one
STEER_KEY = "wake_tools_steer"      # kv: {"run", "tools", "at"} of the wake that made TOOLS_STEER+ calls
STEER_LINE = ("Last wake made {tools} tool calls (run {run}): each call re-reads the whole context. Batch "
              "independent read-only lookups into one shell call (`a; b; c`), and hand reading code or logs "
              "beyond a few short excerpts to a worker or subagent.")
TRIM_KINDS = ("work", "harness")
MECHANICAL = re.compile(r"\b(re-?run|rerun|copy|copies|poll|rename|regenerate|bump|mechanical|scripted|"
                        r"move (?:the )?files?|delete (?:the )?(?:stale|old)|sync)\b", re.I)
CONCLUSION = re.compile(r"^\s*conclusion\s*:", re.I | re.M)
TRIM_LINE = ("Output (rings.output_trim): keep the hand-off and any report short and plain: the result, "
             "the evidence a reader needs to check it, and the follow-ups. No restated spec, no recap of steps, "
             "no padding. Do not drop facts to save words.")


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


# --- lever a: fresh continue -------------------------------------------------------------------------

def review_fixed(task: dict) -> int | None:
    """The review a fix task fixes (its `review_fix:<id>` label), or None."""
    return next((int(x[11:]) for x in task_labels(task) if x.startswith("review_fix:") and x[11:].isdigit()), None)


def _result_text(res: dict) -> str:
    keep = {k: res[k] for k in ("status", "summary", "message", "findings", "followups", "self_check", "on_pass")
            if res.get(k)}
    text = json.dumps(keep, indent=1, ensure_ascii=False, default=str) if keep else ""
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + "\n… (cut)"


def _git(cwd: str, *args: str) -> str:
    try:
        from . import gitmemo
        r = gitmemo.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def handoff_block(p, task_id: int, cwd: str | None) -> str:
    """The compact hand-off a fresh run of `task_id` starts with: its own last hand-off, the hand-off of the
    task it continues, the findings of the review it fixes, and its work so far against the base branch.
    '' when there is nothing. Capped at HANDOFF_CHARS."""
    from . import chaincap
    from .db import load_result
    db = p.db
    task = db.task(int(task_id))
    if not task:
        return ""
    task = dict(task)
    parts: list[str] = []
    if text := _result_text(load_result(task.get("result"))):
        parts.append(f"## This task's last hand-off (an earlier run)\n{text}")
    prev = chaincap.prev_of(task)
    if prev and (pt := db.task(int(prev))) and (text := _result_text(load_result(dict(pt).get("result")))):
        parts.append(f"## Hand-off of #{prev} ({dict(pt).get('title') or ''}), which this task continues\n{text}")
    rev = review_fixed(task)
    if rev and rev != prev and (rt := db.task(rev)) and (text := _result_text(load_result(dict(rt).get("result")))):
        parts.append(f"## Findings of review #{rev}, which this task fixes\n{text}")
    if cwd:
        from . import worktree
        try:
            base = worktree.base_ref(p)
        except Exception:
            base = ""
        stat = _git(cwd, "diff", "--stat", f"{base}...HEAD") if base else ""
        dirty = _git(cwd, "status", "--short")
        if stat.strip() or dirty.strip():
            diff = _git(cwd, "diff", f"{base}...HEAD") if base else ""
            if len(diff) > DIFF_CHARS:
                diff = diff[:DIFF_CHARS] + "\n… (cut: read the rest with git diff)"
            parts.append(f"## Work on this branch so far (against {base or 'the base'})\n{stat.strip()}"
                         + (f"\nuncommitted:\n{dirty.strip()}" if dirty.strip() else "")
                         + (f"\n```diff\n{diff}\n```" if diff.strip() else ""))
    if not parts:
        return ""
    out = ("# Fresh start (rings.fresh_continue)\nThis run starts fresh instead of resuming an earlier worker "
           "session. What earlier runs left, in short (read files again as needed):\n\n" + "\n\n".join(parts))
    return out if len(out) <= HANDOFF_CHARS else out[:HANDOFF_CHARS] + "\n… (cut)"


def _note(raw: Any) -> dict:
    try:
        got = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def arm_of(note: dict) -> str:
    """'fresh' (the lever started it fresh), 'resumed' (it resumed an earlier worker session) or ''."""
    from . import ctxfork
    cc = note.get(ctxfork.KEY)
    if not isinstance(cc, dict):
        return ""
    return "fresh" if cc.get("fresh") else "resumed" if cc.get("how") == "resume" else ""


def fresh_report(p, since: float, until: float | None = None) -> dict:
    """{arm: {runs, usd, tasks, code_tasks, landed, usd_per_landing, landed_rate, tool_calls, review_rounds}}
    plus "landings": {done_code, judged, table_only}: how complete the landing records are."""
    from . import coordstats, landed
    from .db import review_subject
    db = p.db
    until = time.time() if until is None else until
    rows = db.q("SELECT r.id, r.task, r.cost_usd, r.note, r.dir, r.provider, t.kind FROM runs r JOIN tasks t "
                "ON t.id=r.task WHERE r.role!='coordinator' AND r.started>=? AND r.started<? "
                "AND r.status!='running'", (since, until))
    arms: dict[str, dict] = {}
    for r in rows:
        a = arm_of(_note(r["note"]))
        if not a:
            continue
        s = arms.setdefault(a, {"runs": [], "tasks": set(), "code": set()})
        s["runs"].append(r)
        s["tasks"].add(int(r["task"]))
        if r["kind"] == "code":
            s["code"].add(int(r["task"]))
    done = [int(t["id"]) for t in db.q("SELECT id FROM tasks WHERE kind='code' AND status='done' "
                                        "AND updated>=? AND updated<?", (since, until))]
    want = set(done).union(*(s["code"] for s in arms.values())) if arms else set(done)
    rws = landed.Rows(db, want) if want else None
    land = {t: bool(landed.recorded(db, t, rows=rws)[0]) for t in want} if rws else {}
    table = {int(x["task"]) for x in db.q("SELECT DISTINCT task FROM task_landings")}
    subj: dict[int, int] = {}
    for t in db.q("SELECT id, title FROM tasks WHERE kind='review' AND created>=?", (since - WINDOW_S,)):
        if (s := review_subject(dict(t))) is not None:
            subj[s] = subj.get(s, 0) + 1
    out: dict = {}
    for a, s in arms.items():
        code_usd = sum(float(r["cost_usd"] or 0) for r in s["runs"] if int(r["task"]) in s["code"])
        n_land = sum(1 for t in s["code"] if land.get(t))
        calls = [c for r in s["runs"][-SAMPLE:] if (c := coordstats.tool_calls(r, p.runs)) is not None]
        out[a] = {"runs": len(s["runs"]), "usd": round(sum(float(r["cost_usd"] or 0) for r in s["runs"]), 2),
                  "tasks": len(s["tasks"]), "code_tasks": len(s["code"]), "landed": n_land,
                  "usd_per_landing": round(code_usd / n_land, 2) if n_land else None,
                  "landed_rate": round(n_land / len(s["code"]), 3) if s["code"] else None,
                  "tool_calls": round(statistics.median(calls), 1) if calls else None,
                  "review_rounds": round(sum(subj.get(t, 0) for t in s["code"]) / len(s["code"]), 2)
                  if s["code"] else None}
    out["landings"] = {"done_code": len(done), "judged": sum(1 for t in done if land.get(t)),
                       "table_only": sum(1 for t in done if t in table)}
    return out


def fresh_line(p, since: float, until: float | None = None) -> str:
    r = fresh_report(p, since, until)
    lg = r.pop("landings")
    arms = []
    for a in ("fresh", "resumed"):
        if s := r.get(a):
            arms.append(f"{a} {s['runs']} runs"
                        + (f", ${s['usd_per_landing']:.2f}/landing" if s["usd_per_landing"] is not None else "")
                        + (f", landed {100 * s['landed_rate']:.0f}%" if s["landed_rate"] is not None else "")
                        + (f", {s['tool_calls']:g} tool calls/run" if s["tool_calls"] is not None else "")
                        + (f", {s['review_rounds']:g} review rounds/task" if s["review_rounds"] is not None else ""))
    flag = "on" if lever_on(p, "fresh_continue") else "off"
    if not arms and flag == "off":   # nothing to compare: no line
        return ""
    return (f"fresh continue [{flag}]: " + ("; ".join(arms) if arms else "no continue runs")
            + f"; landings: {lg['judged']} of {lg['done_code']} done code tasks landed"
            f" (task_landings alone: {lg['table_only']})")


# --- lever c: output ---------------------------------------------------------------------------------

def effort_pick(p, cfg: dict, task: dict, tier: str, provider: str) -> dict | None:
    """The run note's TRIM_KEY entry for a start of `task`, or None: medium effort for a work or harness task at
    the standard tier with no model or effort pin, no `conclusion:` line and a mechanical spec, when its tier
    runs above medium. Never a code task (effortab), a review, a plan or a question."""
    from . import blindreview, tierfloor
    task = dict(task)
    if task.get("kind") not in TRIM_KINDS or tier != "standard" or str(task.get("effort") or "") \
            or str(task.get("model") or "") or blindreview.is_blind_task(task):
        return None
    spec = str(task.get("spec") or "")
    if CONCLUSION.search(spec) or not (m := MECHANICAL.search(spec)):
        return None
    tier_effort = str((((cfg.get("providers") or {}).get(provider) or {}).get("tiers") or {}).get(tier, {})
                      .get("effort") or "")
    if tierfloor._rank(tier_effort) <= tierfloor._rank("medium"):
        return None
    if not lever_on(p, "output_trim"):
        return None
    return {"effort": "medium", "from": tier_effort, "why": f"mechanical spec ({m.group(0)!r})"}


def prompt_line(p, task: dict) -> str:
    """The worker prompt's output line when the lever is on; '' otherwise (and for blind graders)."""
    from . import blindreview
    if blindreview.is_blind_task(dict(task)) or not lever_on(p, "output_trim"):
        return ""
    return TRIM_LINE


def output_report(p, since: float, until: float | None = None) -> dict:
    """{kind: {runs, out_tokens (median), usd (mean)}} of worker runs, and "trimmed": the same for runs an
    effort pick moved."""
    until = time.time() if until is None else until
    rows = p.db.q("SELECT r.cost_usd, r.output_tokens, r.note, t.kind FROM runs r JOIN tasks t ON t.id=r.task "
                  "WHERE r.role!='coordinator' AND r.started>=? AND r.started<? AND r.status!='running'",
                  (since, until))
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(str(r["kind"] or "?"), []).append(r)
        if isinstance(_note(r["note"]).get(TRIM_KEY), dict):
            groups.setdefault("trimmed", []).append(r)
    return {k: {"runs": len(v), "out_tokens": int(statistics.median([int(r["output_tokens"] or 0) for r in v])),
                "usd": round(sum(float(r["cost_usd"] or 0) for r in v) / len(v), 2)} for k, v in groups.items()}


def output_line(p, since: float, until: float | None = None) -> str:
    r = output_report(p, since, until)
    if not r:
        return ""
    flag = "on" if lever_on(p, "output_trim") else "off"
    kinds = ", ".join(f"{k} {v['out_tokens'] / 1000:.1f}k/${v['usd']:.2f}" for k, v in sorted(r.items())
                      if k != "trimmed")
    trimmed = r.get("trimmed")
    return (f"output trim [{flag}]: worker output tokens/run (median) and $/run: " + (kinds or "no runs")
            + (f"; effort-picked {trimmed['runs']} runs {trimmed['out_tokens'] / 1000:.1f}k/${trimmed['usd']:.2f}"
               if trimmed else ""))


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
    for fn in (fresh_line, lambda p, s, u: wakesettle.line(p.db, s, u), wake_tools_line, output_line,
               eta_shadow_line):
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
    """The daily review's lever lines: the fresh, wake-tools, output and eta shadow metrics (the wake line has
    its own audit line)."""
    out = []
    for fn in (fresh_line, wake_tools_line, output_line, eta_shadow_line):
        try:
            if ln := fn(p, since):
                out.append(ln)
        except Exception as e:
            out.append(f"cost lever metric failed: {type(e).__name__}: {e}"[:200])
    return out
