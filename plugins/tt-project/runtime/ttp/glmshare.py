# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp stats --glm-share`: how much of a project's model time runs on GLM, and whether its quality holds.
Model-free: it reads the project's own runs, tasks, events, ledger and kv.

Share (report): every run started in the window, coordinator turns included, by role (ROLES: coordinator, a
worker run's task kind, device for device work (tierfloor.device_run), other for a run with no task) as
model-hours (wall time, started to ended or the window's end) and as runs. A run is on GLM when it ran on the
chat provider (route.TO) with a model whose name has "glm" in any case: the runs row names the provider a
fallback ran on, and a mid-run swap counts its stand-in (modelrank.ran).

Quality per arm: each worker task (not a review) whose first worker run started in the window is in one arm
(arm_of): its glm_work arm (kv glm_work_arm:<id>: glm, or holdout = claude), else its code arm (kv
code_arm:<id>: glm, else claude), else glm when its first worker run was on GLM, else claude. Per arm: tasks,
done, failed, reopened (an explicit requeue after it ended), the first auto review's pass rate, fix rounds
(review fix hops) per reviewed task, bounces (a needs_opus fallback or a needs_deep hand-off), device incidents
on the arm's GLM runs (deviceab.incident_event) and tasks done per day. Beside them: Claude $/day (the ledger's
claude spend) and user corrections (correction.py's kv, kept 7 days): a correction names no task, so it is a
project count, not per arm.

Wave gate (judge): the project's GLM arm (kv glm_work_arm arm glm) against its own Claude baseline: its holdout
tasks plus the tasks whose first worker run started in the BASE_DAYS before its first arm record, off GLM.
"wait" until `route.glm_work.min_tasks` (MIN_TASKS) GLM tasks have an outcome (their first review decided, or
ended with no review open) and the baseline has BASE_MIN; "worse" when the GLM review first-pass rate is lower
by more than `route.glm_work.worse_pts` (WORSE_PTS) points, its failed-or-reopened rate higher by more, or any
device incident on a GLM run; else "ok".

Bad output by day (bad_output): each GLM run's exit record (the chat bridge's stats, chatbridge.JUNK_PATTERNS
and the counters beside it) summed per home-zone day of its start: tool calls Claude Code rejected for bad
input, tool arguments that did not parse, answers with reasoning only, and answers holding junk; each with
its rate per answer (the answers the bridge scanned, usage.scanned) and the runs it hit. A run with no exit
record (still running, or killed) or from a bridge before these counters is not counted, never shown as clean. The daily review lists the last BAD_DAYS days.
"""
from __future__ import annotations

import bisect
import datetime
import json
from pathlib import Path

from . import glmassist, route

ROLES = ("coordinator", "plan", "review", "code", "work", "question", "harness", "device", "other")
KINDS = ("plan", "review", "code", "work", "question", "harness")
ARM_KV = "glm_work_arm:"          # kv glm_work_arm:<task id>: {"arm": "glm" | "holdout", "why", "ts"}
CODE_ARM_KV = "code_arm:"         # codearm.ARM_KV
MIN_TASKS, WORSE_PTS, BASE_DAYS, BASE_MIN = 25, 15.0, 14, 5
CHUNK = 500
BAD_KEYS = ("tool_input_errors", "args_unparsed", "args_repaired", "empty_content", "junk_responses")
LOOP_KEYS = {"loop_nudges": "nudges", "loop_stops": "stops"}   # chatbridge stats loop (the loop guard)   # chatbridge stats bad_output
BAD_DAYS = 7


def on_glm(provider, model: str, note) -> bool:
    """The run ran on the chat provider with a GLM model (a mid-run swap: its stand-in)."""
    from . import modelrank
    return provider == route.TO and "glm" in modelrank.ran(str(model or ""), note)[0].lower()


def _note(raw) -> dict:
    try:
        v = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _labels(task: dict) -> list[str]:
    try:
        v = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        return []
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def role_of(run: dict, task: dict | None, cfg: dict, cache: dict) -> str:
    if run.get("role") == "coordinator":
        return "coordinator"
    if not task:
        return "other"
    kind = str(task.get("kind") or "")
    if kind == "review":
        return "review"
    if task["id"] not in cache:
        from .tierfloor import device_run
        cache[task["id"]] = device_run(task, cfg)
    if cache[task["id"]]:
        return "device"
    return kind if kind in KINDS else "other"


def _tasks(db, ids) -> dict[int, dict]:
    ids, out = sorted({int(i) for i in ids if i is not None}), {}
    for i in range(0, len(ids), CHUNK):
        part = ids[i:i + CHUNK]
        out.update({t["id"]: t for t in db.q(f"SELECT * FROM tasks WHERE id IN ({','.join('?' * len(part))})", part)})
    return out


def _worker_runs(db, ids) -> dict[int, list[dict]]:
    ids, out = sorted(ids), {}
    for i in range(0, len(ids), CHUNK):
        part = ids[i:i + CHUNK]
        for r in db.q(f"SELECT id, task, provider, model, status, started, ended, note FROM runs WHERE role='worker' "
                      f"AND task IN ({','.join('?' * len(part))}) ORDER BY id", part):
            out.setdefault(r["task"], []).append(r)
    return out


def arm_records(db, prefix: str = ARM_KV) -> dict[int, dict]:
    """{task id: record} of the kv records under `prefix`."""
    out = {}
    for r in db.q("SELECT key, value FROM kv WHERE key LIKE ?", (f"{prefix}%",)):
        tail, rec = str(r["key"])[len(prefix):], _note(r["value"])
        if tail.isdigit() and rec.get("arm"):
            out[int(tail)] = rec
    return out


def arm_of(tid: int, runs: list[dict], work: dict, code: dict) -> str:
    """glm or claude (arm_of in the module doc)."""
    if tid in work:
        return "glm" if work[tid].get("arm") == "glm" else "claude"
    if tid in code:
        return "glm" if code[tid].get("arm") == "glm" else "claude"
    return "glm" if runs and on_glm(runs[0]["provider"], runs[0]["model"], runs[0]["note"]) else "claude"


def _reviews(db, since: float) -> dict[int, dict]:
    """{task id: {"first": done | failed | None, "open": bool}} from its auto reviews created since `since`."""
    out: dict[int, dict] = {}
    for r in db.q("SELECT id, status, labels FROM tasks WHERE kind='review' AND labels LIKE '%\"auto_review:%' "
                  "AND created>=? ORDER BY id", (since,)):
        for lb in _labels(r):
            if lb.startswith("auto_review:") and lb[12:].isdigit():
                v = out.setdefault(int(lb[12:]), {"first": None, "open": False})
                if r["status"] in ("done", "failed") and v["first"] is None:
                    v["first"] = r["status"]
                v["open"] |= r["status"] not in ("done", "failed", "cancelled")
    return out


def _fix_rounds(db, since: float) -> dict[int, int]:
    """{root task id: review fix hops} of the review fixes created since `since` (chaincap's lineage)."""
    from . import chaincap
    out: dict[int, int] = {}
    for t in db.q("SELECT * FROM tasks WHERE kind!='review' AND created>=? AND "
                  "(labels LIKE '%\"review_fix:%' OR title LIKE '%fix review #%')", (since,)):
        if not chaincap.is_review_fix(t):
            continue
        cur, seen = t, set()
        while cur and cur["id"] not in seen and chaincap.is_review_fix(cur):
            seen.add(cur["id"])
            prev = chaincap.prev_of(cur)
            cur = db.task(prev) if prev is not None else None
        if cur:
            out[cur["id"]] = out.get(cur["id"], 0) + 1
    return out


def _incidents(db, cfg: dict, runs: list[dict], tasks: dict, until: float) -> int:
    """Runs of `runs` with a device incident during them or within deviceab.INCIDENT_AFTER_S after."""
    from . import deviceab
    if not runs:
        return 0
    lo = min(float(r["started"] or 0) for r in runs)
    kinds = sorted(deviceab.INCIDENT_KINDS)
    ev = db.q(f"SELECT id, ts, source, kind, text, data, task FROM events WHERE ts>=? AND ts<=? AND kind NOT LIKE "
              f"'device_ab%' AND (kind='observation' OR kind LIKE 'device%' OR kind LIKE 'broker%' OR kind IN "
              f"({','.join('?' * len(kinds))})) ORDER BY ts", (lo, until + deviceab.INCIDENT_AFTER_S, *kinds))
    ts, n, names = [float(e["ts"]) for e in ev], 0, {}
    for r in runs:
        t = tasks.get(r["task"]) or {}
        if r["task"] not in names:
            names[r["task"]] = deviceab.device_names(t, cfg)
        a = float(r["started"] or 0)
        b = float(r["ended"] or until) + deviceab.INCIDENT_AFTER_S
        n += any(deviceab.incident_event(e, r["task"], names[r["task"]], r["id"])
                 for e in ev[bisect.bisect_left(ts, a):bisect.bisect_right(ts, b)])
    return n


def quality(db, cfg: dict, tids, tasks: dict, runs: dict, reviews: dict, fixes: dict, requeued: set,
            days: float, until: float) -> dict:
    """One arm's quality over tasks `tids` (the module doc)."""
    from . import effort
    q = {"tasks": 0, "done": 0, "failed": 0, "reopened": 0, "reviewed": 0, "first_pass": 0, "fix_rounds": 0,
         "bounces": 0, "decided": 0, "glm_runs": 0, "incidents": 0}
    glm_runs = []
    for tid in tids:
        t = tasks.get(tid)
        if not t:
            continue
        rs, rv = runs.get(tid) or [], reviews.get(tid) or {}
        q["tasks"] += 1
        q["done"] += t["status"] == "done"
        q["failed"] += t["status"] == "failed"
        q["reopened"] += tid in requeued
        if rv.get("first"):
            q["reviewed"] += 1
            q["first_pass"] += rv["first"] == "done"
            q["fix_rounds"] += fixes.get(tid, 0)
        q["decided"] += bool(rv.get("first")) or (t["status"] in ("done", "failed") and not rv.get("open"))
        q["bounces"] += any(((_note(r["note"]).get(route.KEY) or {}).get("fallback")) == route.BOUNCE for r in rs) \
            or effort.wants_deep(t.get("result"))
        glm_runs += [r for r in rs if on_glm(r["provider"], r["model"], r["note"])]
    q["glm_runs"] = len(glm_runs)
    q["incidents"] = _incidents(db, cfg, glm_runs, tasks, until)
    rate = lambda a, b: round(100.0 * a / b, 1) if b else None   # noqa: E731
    q["first_pass_pct"] = rate(q["first_pass"], q["reviewed"])
    q["bad_pct"] = rate(q["failed"] + q["reopened"], q["decided"])
    q["fix_rounds_per_reviewed"] = round(q["fix_rounds"] / q["reviewed"], 2) if q["reviewed"] else None
    q["done_per_day"] = round(q["done"] / days, 2) if days > 0 else None
    return q


def _requeued(db, since: float) -> set[int]:
    from .dispatchguard import REQUEUED
    return {int(r["task"]) for r in db.q("SELECT task, text FROM events WHERE kind=? AND ts>=? AND task IS NOT NULL",
                                         (REQUEUED, since))
            if " from done" in str(r["text"] or "") or " from failed" in str(r["text"] or "")}


def report(p, since: float, now: float) -> dict:
    """The project's GLM share by role and quality per arm over [since, now) (the module doc)."""
    days, db, cfg = max(0.0, now - since) / 86400, p.db, p.config()
    rows = db.q("SELECT id, task, role, provider, model, started, ended, note, dir FROM runs WHERE started>=? AND started<?",
                (since, now))
    tasks = _tasks(db, (r["task"] for r in rows))
    roles = {k: {"runs": 0, "glm_runs": 0, "hours": 0.0, "glm_hours": 0.0} for k in ROLES}
    dev: dict = {}
    for r in rows:
        v = roles[role_of(r, tasks.get(r["task"]), cfg, dev)]
        h = max(0.0, min(float(r["ended"] or now), now) - float(r["started"] or now)) / 3600
        g = on_glm(r["provider"], r["model"], r["note"])
        # A GLM run's ask-Opus minutes (glmassist.py) count on the Claude side.
        adv = min(h, glmassist.hours(p.runs / str(r["id"]))) if g and '"glm_assist"' in (r["note"] or "") else 0.0
        v["runs"] += 1
        v["glm_runs"] += g
        v["hours"] += h
        v["glm_hours"] += (h - adv) * g
    total = {k: sum(v[k] for v in roles.values()) for k in ("runs", "glm_runs", "hours", "glm_hours")}
    for v in (*roles.values(), total):
        v["hours"], v["glm_hours"] = round(v["hours"], 3), round(v["glm_hours"], 3)
        v["hour_pct"] = round(100.0 * v["glm_hours"] / v["hours"], 1) if v["hours"] else None
        v["run_pct"] = round(100.0 * v["glm_runs"] / v["runs"], 1) if v["runs"] else None
    work_ids = {t for t, x in tasks.items() if x.get("kind") != "review"}
    runs = _worker_runs(db, work_ids)
    firsts = {t for t in work_ids if runs.get(t) and since <= float(runs[t][0]["started"] or 0) < now}
    work, code = arm_records(db), arm_records(db, CODE_ARM_KV)
    by_arm: dict[str, list[int]] = {"glm": [], "claude": []}
    for t in sorted(firsts):
        by_arm[arm_of(t, runs[t], work, code)].append(t)
    ctx = (_reviews(db, since), _fix_rounds(db, since), _requeued(db, since))
    arms = {a: quality(db, cfg, ids, tasks, runs, *ctx, days, now) for a, ids in by_arm.items()}
    usd = (db.one("SELECT SUM(usd) s FROM ledger WHERE provider='claude' AND ts>=? AND ts<?", (since, now)) or {}).get("s")
    from . import correction
    corr = [c for c in (db.kv(correction.KEY, []) or []) if isinstance(c, dict) and since <= float(c.get("ts") or 0) < now]
    return {"project": p.name, "since": since, "until": now, "days": days, "total": total, "roles": roles,
            "arms": arms, "claude_usd_per_day": round(float(usd or 0) / days, 2) if days > 0 else None,
            "corrections": {"n": len(corr), "per_arm": None,
                            "note": "a correction names no task, so it is counted per project; kept 7 days"},
            "bad_output": bad_output(p, rows)}


def _bridge_of(p, r) -> dict | None:
    """A run's exit-record bridge stats ({} when it had none), None when it has no exit record yet."""
    d = Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])
    try:
        got = json.loads((d / "exit.json").read_text())
    except (OSError, ValueError):
        return None
    b = got.get("bridge") if isinstance(got, dict) else None
    return b if isinstance(b, dict) else {}


def bad_output(p, rows) -> list[dict]:
    """The GLM runs' bad output per home-zone day, oldest first (the module doc)."""
    from . import timefmt
    tz, days = timefmt.zone(p), {}
    for r in rows:
        b = _bridge_of(p, r) if on_glm(r["provider"], r["model"], r["note"]) else None
        scanned = (b or {}).get("usage", {}).get("scanned") if isinstance((b or {}).get("usage"), dict) else None
        if not isinstance(scanned, int):
            continue   # no exit record, or a bridge from before the counters: not measured, never a zero
        day = datetime.datetime.fromtimestamp(float(r["started"] or 0), tz).strftime("%Y-%m-%d")
        v = days.setdefault(day, {"day": day, "runs": 0, "answers": 0, "runs_hit": 0,
                                  **{k: 0 for k in (*BAD_KEYS, *LOOP_KEYS)}})
        bad = b.get("bad_output") if isinstance(b.get("bad_output"), dict) else {}
        n = {k: int(bad.get(k) or 0) for k in BAD_KEYS}
        loop = b.get("loop") if isinstance(b.get("loop"), dict) else {}
        n.update({k: int(loop.get(f) or 0) for k, f in LOOP_KEYS.items()})
        v["runs"] += 1
        v["answers"] += scanned
        v["runs_hit"] += any(n.values())
        for k in n:
            v[k] += n[k]
    for v in days.values():
        v["pct"] = {k: round(100.0 * v[k] / v["answers"], 2) if v["answers"] else None for k in BAD_KEYS}
    return [days[d] for d in sorted(days)]


def bad_lines(got: list[dict]) -> list[str]:
    """One line per day of bad_output's list."""
    pct = lambda v: "-" if v is None else f"{v:g}%"   # noqa: E731
    return [f"{v['day']}: {v['runs']} GLM runs, {v['answers']} answers; tool input errors {v['tool_input_errors']} "
            f"({pct(v['pct']['tool_input_errors'])}), args unparsed {v['args_unparsed']} ({pct(v['pct']['args_unparsed'])}, "
            f"mended {v['args_repaired']}), "
            f"reasoning-only {v['empty_content']} ({pct(v['pct']['empty_content'])}), junk {v['junk_responses']} "
            f"({pct(v['pct']['junk_responses'])}); loop nudges {v.get('loop_nudges', 0)}, loop stops "
            f"{v.get('loop_stops', 0)}; runs hit {v['runs_hit']}" for v in got]


def daily_lines(p, now: float) -> list[str]:
    """The daily review's lines: the last BAD_DAYS days of bad_output; [] with no GLM run in them."""
    since = now - BAD_DAYS * 86400
    rows = p.db.q("SELECT id, task, role, provider, model, started, ended, note, dir FROM runs WHERE started>=? "
                  "AND started<?", (since, now))
    return bad_lines(bad_output(p, rows))


def text(s: dict) -> str:
    t = s["total"]
    pct = lambda v: "-" if v is None else f"{v:g}%"   # noqa: E731
    lines = [f"{s['project']}: GLM {pct(t['hour_pct'])} of {t['hours']:.1f} model-h, {pct(t['run_pct'])} of "
             f"{t['runs']} runs ({s['days']:g} d)"]
    parts = [f"{k} {pct(v['hour_pct'])} of {v['hours']:.1f}h" for k, v in s["roles"].items() if v["runs"]]
    if parts:
        lines.append("  GLM share of hours by role: " + "; ".join(parts))
    for a, q in s["arms"].items():
        lines.append(f"  {a}: {q['tasks']} tasks, {q['done']} done ({q['done_per_day'] or 0:g}/day), {q['failed']} failed, "
                     f"{q['reopened']} reopened, first review passed {pct(q['first_pass_pct'])} of {q['reviewed']}, "
                     f"fix rounds/reviewed {q['fix_rounds_per_reviewed'] if q['fix_rounds_per_reviewed'] is not None else '-'}, "
                     f"{q['bounces']} bounced, {q['incidents']} device incidents on GLM runs")
    lines.append(f"  Claude ${s['claude_usd_per_day'] or 0:.2f}/day; user corrections {s['corrections']['n']} "
                 "(per project: a correction names no task)")
    if s.get("bad_output"):
        lines.append("  GLM bad output by day (rate per answer):")
        lines += [f"    {x}" for x in bad_lines(s["bad_output"])]
    return "\n".join(lines)


def settings(cfg: dict) -> dict:
    """route.glm_work's min_tasks and worse_pts, else MIN_TASKS and WORSE_PTS."""
    raw = ((cfg.get("route") or {}).get("glm_work")) if isinstance(cfg.get("route"), dict) else None
    raw = raw if isinstance(raw, dict) else {}
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0   # noqa: E731
    return {"min_tasks": int(raw["min_tasks"]) if num(raw.get("min_tasks")) else MIN_TASKS,
            "worse_pts": float(raw["worse_pts"]) if num(raw.get("worse_pts")) else WORSE_PTS}


def judge(p, cfg: dict, now: float) -> dict:
    """{"state": "ok" | "worse" | "wait", "why", "n"}: the project's GLM arm against its own Claude baseline (the
    module doc); n is the GLM tasks with an outcome."""
    db, st = p.db, settings(cfg)
    recs = arm_records(db)
    if not recs:
        return {"state": "wait", "why": "no glm_work arm records yet", "n": 0}
    t0 = min(float(r.get("ts") or now) for r in recs.values())
    since = t0 - BASE_DAYS * 86400
    pre = {r["task"]: r for r in db.q("SELECT task, MIN(started) s FROM runs WHERE role='worker' AND task IS NOT NULL "
                                      "GROUP BY task HAVING s>=? AND s<?", (since, t0))}
    ids = set(recs) | set(pre)
    tasks, runs = _tasks(db, ids), _worker_runs(db, ids)
    glm = [t for t, r in recs.items() if r.get("arm") == "glm"]
    base = [t for t, r in recs.items() if r.get("arm") == "holdout"] + [
        t for t in pre if t not in recs and (tasks.get(t) or {}).get("kind") != "review" and runs.get(t)
        and not on_glm(runs[t][0]["provider"], runs[t][0]["model"], runs[t][0]["note"])]
    ctx = (_reviews(db, since), _fix_rounds(db, since), _requeued(db, since))
    days = max((now - t0) / 86400, 1e-9)
    g = quality(db, cfg, glm, tasks, runs, *ctx, days, now)
    b = quality(db, cfg, base, tasks, runs, *ctx, days, now)
    n = g["decided"]
    nums = (f"GLM first review passed {g['first_pass_pct']}% of {g['reviewed']}, failed or reopened {g['bad_pct']}% "
            f"of {n}; Claude baseline {b['first_pass_pct']}% of {b['reviewed']}, {b['bad_pct']}% of {b['decided']}")
    if n < st["min_tasks"]:
        return {"state": "wait", "why": f"{n} of {st['min_tasks']} GLM tasks with an outcome; {nums}", "n": n}
    if g["incidents"]:
        return {"state": "worse", "why": f"{g['incidents']} device incident(s) on GLM runs; {nums}", "n": n}
    if b["decided"] < BASE_MIN:
        return {"state": "wait", "why": f"the Claude baseline has {b['decided']} tasks with an outcome (needs "
                                         f"{BASE_MIN}); {nums}", "n": n}
    m = st["worse_pts"]
    if g["first_pass_pct"] is not None and b["first_pass_pct"] is not None and b["reviewed"] >= BASE_MIN \
            and g["first_pass_pct"] < b["first_pass_pct"] - m:
        return {"state": "worse", "why": f"first review pass rate lower by more than {m:g} points; {nums}", "n": n}
    if g["bad_pct"] is not None and b["bad_pct"] is not None and g["bad_pct"] > b["bad_pct"] + m:
        return {"state": "worse", "why": f"failed or reopened rate higher by more than {m:g} points; {nums}", "n": n}
    return {"state": "ok", "why": f"within {m:g} points of the Claude baseline; {nums}", "n": n}
