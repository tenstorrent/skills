# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp stats --glm-share`: how much of a project's model time runs on GLM, and whether its quality holds.
Model-free: it reads the project's own runs, tasks, events, ledger and kv.

Share (report): every run started in the window, coordinator turns included, by role (ROLES: coordinator, a
worker run's task kind, device for device work (tierfloor.device_run), other for a run with no task) as
model-hours (wall time, started to ended or the window's end) and as runs. A run is on GLM when it ran on the
chat provider (route.TO) with a model whose name has "glm" in any case: the runs row names the provider a
fallback ran on, and a mid-run swap counts its stand-in (modelrank.ran).

Held off GLM (held): the runs of eligible kinds (code, work, question, harness) that ran on Claude in the
window, plus coordinator, plan and review runs, grouped by the rule that kept them there (KEEPERS: the user's
own rule, or a removable one), as model-hours and runs, with the share of the window's hours. A code run's
reason is its recorded code arm stay, any other run's the route entry's fallback (a lineage's misses, a
rerun) or the keep-core check run again on its task (glmwork.keeps_core), so a run that predates a rule is
named by today's rules; a run none of them explains is "unrecorded".

Quality per arm: each worker task (not a review) whose first worker run started in the window is in one arm
(arm_of): its glm_work arm (kv glm_work_arm:<id>: glm, or holdout = claude), else its code arm (kv
code_arm:<id>: glm, else claude), else glm when its first worker run was on GLM, else claude. Per arm: tasks,
done, failed, reopened (an explicit requeue after it ended), the first auto review's pass rate, fix rounds
(review fix hops) per reviewed task, bounces (a needs_opus fallback or a needs_deep hand-off), device incidents
on the arm's GLM runs (deviceab.incident_event) and tasks done per day. Beside them: Claude $/day (the ledger's
claude spend) and user corrections (correction.py's kv, kept 7 days): a correction names no task, so it is a
project count, not per arm.

Wave gate (judge): the project's GLM arm (kv glm_work_arm arm glm, armed since its last re-arm) against its own
Claude baseline from the same period: its holdout tasks and the tasks off GLM whose first worker run started since
BASE_DAYS before its first arm record, counted only when their outcome (the first auto review's queue time, else
the end of their last run) falls in the GLM window being judged; with fewer than `min_window` (BASE_WINDOW_MIN)
reviewed ones there, the window reaches back only to the newest `min_window` reviewed outcomes. A review rule
that got stricter partway through then weighs on both arms alike. The verdict carries both first review pass
rates ("glm_pct", "base_pct") and the baseline window ("base_window": [from, to]). It
answers "worse" (roll the project back), "ok" (it may advance) or "wait" (hold the wave where it is). Every
setting is under `route.glm_work` (settings()); a signal is "worse by" the gap in the direction that hurts.
- Any device incident on a GLM run: "worse" at once, whatever the sample (a safety stop, not a statistic).
- Minimum sample: below `min_tasks` (MIN_TASKS) GLM tasks with an outcome (their first review decided, or ended
  with no review open) or `min_base` (BASE_MIN) baseline ones, "wait" with "not enough data", so noise on a small
  holdout neither advances nor rolls back a wave. The review signals also need `min_base` reviewed tasks in each
  arm; with fewer they are reported as not measured and gate nothing.
- Advance and rollback (GATED): first review pass rate and failed-or-reopened rate (points, `advance_pts`
  ADVANCE_PTS, `rollback_pts` ROLLBACK_PTS; the older `worse_pts` sets rollback_pts), fix rounds per reviewed
  task (rounds, `advance_rounds` ADVANCE_ROUNDS, `rollback_rounds` ROLLBACK_ROUNDS) and the bounce rate (points
  of tasks bounced to Opus or deep, `advance_bounce_pts` ADVANCE_BOUNCE_PTS, `rollback_bounce_pts`
  ROLLBACK_BOUNCE_PTS: a bounce still ends on Opus, so it costs time and money before quality, hence the wider
  margins). Worse by more than the rollback margin: "worse"; by more than the tighter advance margin: "wait".
- Advance only (HOLD_ONLY): the project's tasks done per day since the first arm record against the BASE_DAYS
  before it (a percent drop, `hold_done_pct` HOLD_DONE_PCT; needs `min_base` tasks before) and user corrections
  in the last CORR_DAYS days (more than `hold_corrections` HOLD_CORRECTIONS). Both are per project and move with
  the user's own workload, so they hold a wave but never roll one back.
- glm_code (judge_code): the same judge over the code arm's own records (codearm.ARM_KV), on its three
  metrics alone: first review pass rate, bounce rate and tasks done per day.
- Reported only: Claude $/day and bad output by day (report()).
The verdict lists each signal ("signals": glm, base, worse_by, verdict ok | hold | worse | unmeasured).

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
import re
from pathlib import Path

from . import codearm, costlevers, glmassist, route, runclass
from .db import load_result
from .util import task_labels

ROLES = ("coordinator", "plan", "review", "code", "work", "question", "harness", "device", "other")
KINDS = ("plan", "review", "code", "work", "question", "harness")
ARM_KV = "glm_work_arm:"          # kv glm_work_arm:<task id>: {"arm": "glm" | "holdout", "why", "ts"}
CODE_ARM_KV = "code_arm:"         # codearm.ARM_KV
MIN_TASKS, BASE_DAYS, BASE_MIN = 25, 14, 10
BASE_WINDOW_MIN = 20   # reviewed Claude outcomes the same-period baseline holds before it reaches back further
ADVANCE_PTS, ROLLBACK_PTS = 5.0, 15.0
ADVANCE_ROUNDS, ROLLBACK_ROUNDS = 0.25, 0.5
ADVANCE_BOUNCE_PTS, ROLLBACK_BOUNCE_PTS = 10.0, 25.0
HOLD_DONE_PCT, HOLD_CORRECTIONS, CORR_DAYS = 25.0, 3, 7
# The wave gate's signals: (name, quality() key, higher is worse, advance setting, rollback setting).
GATED = (("first review pass rate", "first_pass_pct", False, "advance_pts", "rollback_pts"),
         ("failed or reopened rate", "bad_pct", True, "advance_pts", "rollback_pts"),
         ("fix rounds per reviewed task", "fix_rounds_per_reviewed", True, "advance_rounds", "rollback_rounds"),
         ("bounce rate", "bounce_pct", True, "advance_bounce_pts", "rollback_bounce_pts"))
CODE_METRICS = frozenset(("first review pass rate", "bounce rate", "tasks done per day"))
   # the glm_code flag gates on these alone (glmshare.judge_code): the code route's own quality, not the
   # work route's bad-output or fix-round signals
REVIEW_KEYS = ("first_pass_pct", "fix_rounds_per_reviewed")   # need min_base reviewed tasks in each arm
CHUNK = 500
BAD_KEYS = ("tool_input_errors", "args_unparsed", "args_repaired", "empty_content", "junk_responses")
# glmguard's fires, read from the run folder: guard_bounces are refusals that handed the run back to Opus (HIT), the
# bounces the rollout judges count; guard_redirects are refusals the run was told the allowed way for and went on.
FIRE_KEYS = ("outward_drafts", "gate_stops", "guard_bounces", "guard_redirects")
LOOP_KEYS = {"loop_nudges": "nudges", "loop_stops": "stops"}   # chatbridge stats loop (the loop guard)
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
    """{task id: {"first": done | failed | None, "at": the first deciding review's created time, "open": bool,
    "code_finding": the first failed review found a bug in the code (review_failure substantive or mixed)}}
    from its auto reviews created since `since`."""
    out: dict[int, dict] = {}
    for r in db.q("SELECT id, status, labels, created, result FROM tasks WHERE kind='review' AND labels LIKE '%\"auto_review:%' "
                  "AND created>=? ORDER BY id", (since,)):
        for lb in task_labels(r):
            if lb.startswith("auto_review:") and lb[12:].isdigit():
                v = out.setdefault(int(lb[12:]), {"first": None, "at": None, "open": False})
                if r["status"] in ("done", "failed") and v["first"] is None:
                    v["first"], v["at"] = r["status"], float(r["created"] or 0)
                    v["code_finding"] = (r["status"] == "failed"
                                         and load_result(r["result"]).get("review_failure") in ("substantive", "mixed"))
                v["open"] |= r["status"] not in ("done", "failed", "cancelled")
    return out


def probes_on_since(p) -> float:
    """When the glm_probes flag last turned on for `p`: its own setting's last change, else the last rollout
    change that took the flag from a level not covering `p`'s ring to one that does; 0 when none is recorded.
    A task made before then ran without the probe step, whatever its id's parity."""
    from . import configwho, rollout
    key = rollout.FLAGS["glm_probes"]["key"]
    if rollout.own_setting(p.raw_config(), key):
        rows = [r for r in configwho.history(p, key) if not r.get("refused") and "new" in r]
        return float(rows[-1]["ts"]) if rows else 0.0
    ring = rollout.ring_of(p.name)

    def covered(v) -> bool:
        return v in (0, 1, 2) and not isinstance(v, bool) and ring is not None and ring <= v
    return max((float(r.get("ts") or 0) for r in rollout.ledger("glm_probes")
                if r.get("kind") in ("set", "promote", "arm") and covered(r.get("to")) and not covered(r.get("from"))),
               default=0.0)


def probe_arms(ids, tasks: dict, reviews: dict, since: float = 0.0) -> dict:
    """The adversarial-probe A/B (codearm.probe_arm) over the GLM code tasks `ids` made since `since`, per arm: those with a first
    review, how many failed it, how many of those failed on a finding in the code (the share logic bugs make of the
    failures; the review's own `review_failure`, no model), and both as percents."""
    out = {a: {"reviewed": 0, "failed": 0, "code_findings": 0} for a in codearm.PROBE_ARMS}
    for t in ids:
        rv = reviews.get(t) or {}
        if (tasks.get(t) or {}).get("kind") != "code" or not rv.get("first") or float(tasks[t]["created"] or 0) < since:
            continue
        v = out[codearm.probe_arm(t)]
        v["reviewed"] += 1
        v["failed"] += rv["first"] == "failed"
        v["code_findings"] += bool(rv.get("code_finding"))
    for v in out.values():
        v["first_fail_pct"] = round(100.0 * v["failed"] / v["reviewed"], 1) if v["reviewed"] else None
        v["logic_bug_pct"] = round(100.0 * v["code_findings"] / v["failed"], 1) if v["failed"] else None
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
            days: float, until: float, fixed: dict | None = None, arm: str = "glm") -> dict:
    """One arm's quality over tasks `tids` (the module doc); a bounce `fixed` (rollout.fixed_stops) lists from
    before its fix went live is not counted: a task whose every bounce that is lands in q["not_counted"]. A
    task of the GLM arm that bounced and whose last run finished on the core provider keeps only its bounce
    count (a retry after GLM's own failed or reopened work still counts against GLM): its outcome is Opus's,
    not GLM's, so it moves none of the outcome metrics."""
    from . import effort, rollout
    q = {"tasks": 0, "done": 0, "failed": 0, "reopened": 0, "reviewed": 0, "first_pass": 0, "fix_rounds": 0,
         "bounces": 0, "decided": 0, "glm_runs": 0, "incidents": 0}
    glm_runs, q["not_counted"] = [], []
    for tid in tids:
        t = tasks.get(tid)
        if not t:
            continue
        rs, rv = runs.get(tid) or [], reviews.get(tid) or {}
        bounced = [r for r in rs if (_note(r["note"]).get(route.KEY) or {}).get("fallback") == route.BOUNCE]
        finished_off = (arm == "glm" and bounced and not on_glm(rs[-1]["provider"], rs[-1]["model"], rs[-1]["note"])
                        and (not rv.get("at") or float(bounced[0]["started"] or 0) < rv["at"]))   # Opus finished it before its first review
        q["tasks"] += 1
        if not finished_off:
            q["done"] += t["status"] == "done"
            q["failed"] += t["status"] == "failed"
            q["reopened"] += tid in requeued
            if rv.get("first"):
                q["reviewed"] += 1
                q["first_pass"] += rv["first"] == "done"
                q["fix_rounds"] += fixes.get(tid, 0)
            q["decided"] += _outcome_at(t, rv, rs) is not None
        off = [f for r in bounced if (f := rollout.not_counted(fixed or {}, r["id"], r["ended"] or r["started"]))]
        deep = effort.wants_deep(t.get("result"))
        q["not_counted"] += off if off and len(off) == len(bounced) and not deep else []   # the task's bounce it took out
        q["bounces"] += len(off) < len(bounced) or deep
        glm_runs += [r for r in rs if on_glm(r["provider"], r["model"], r["note"])]
    q["glm_runs"] = len(glm_runs)
    q["incidents"] = _incidents(db, cfg, glm_runs, tasks, until)
    rate = lambda a, b: round(100.0 * a / b, 1) if b else None   # noqa: E731
    q["first_pass_pct"] = rate(q["first_pass"], q["reviewed"])
    q["bad_pct"] = rate(q["failed"] + q["reopened"], q["decided"])
    q["fix_rounds_per_reviewed"] = round(q["fix_rounds"] / q["reviewed"], 2) if q["reviewed"] else None
    q["bounce_pct"] = rate(q["bounces"], q["tasks"])
    q["done_per_day"] = round(q["done"] / days, 2) if days > 0 else None
    return q


def _requeued(db, since: float) -> set[int]:
    from .dispatchguard import REQUEUED
    return {int(r["task"]) for r in db.q("SELECT task, text FROM events WHERE kind=? AND ts>=? AND task IS NOT NULL",
                                         (REQUEUED, since))
            if " from done" in str(r["text"] or "") or " from failed" in str(r["text"] or "")}


# What keeps eligible work on Claude: the user's own rule (True: deep thinking, optimization or design, a run that
# talks to a person, a review or plan, the Claude holdout, outside text, an explicit pin or a bounce back for Opus)
# or a rule that is not (False).
KEEPERS = {"coordinator turn": True, "review": True, "plan": True, "deep": True, "needs_opus bounce": True,
           "design or optimization": True, "person": True, "person:reply_chat": True, "person:instruction": True,
           "person:conclusion": True, "holdout": True, "untrusted": True, "pin": True,
           "bounced": True, "device": True, "policy files": False, "lineage misses": False, "rerun after a fallback": False,
           "conclusion floor": False, "verdict wording": False, "retry": False, "urgent": False, "kind off the list": False,
           "unrecorded": False}
_WHY = (("policy", "policy files"), ("lineage", "lineage misses"), ("needs opus", "needs_opus bounce"),
        ("needs_opus", "needs_opus bounce"), ("person", "person"), ("untrusted", "untrusted"),
        ("conclusion", "conclusion floor"), ("design", "design or optimization"), ("optim", "design or optimization"),
        ("verdict", "verdict wording"), ("deep", "deep"), ("retry", "retry"), ("urgent", "urgent"), ("pin", "pin"),
        ("device", "device"), ("bounced", "bounced"), ("holdout", "holdout"), ("user_ordered", "user-ordered"))


def _person(task: dict) -> str:
    """Which way a task reaches a person (the `person:` keeper names; old rows keep the bare "person"): a chat
    reply, an instruction to post or notify, else only a `conclusion:` line (which keeps nothing now)."""
    from . import codearm, glmwork
    if task.get("reply_chat"):
        return "person:reply_chat"
    if glmwork.PERSON.search(f"{task.get('title') or ''}\n{task.get('spec') or ''}"):
        return "person:instruction"
    return "person:conclusion" if codearm.CONCLUSION.search(str(task.get("spec") or "")) else ""


def keeper(p, cfg: dict, run: dict, task: dict | None, dev: dict) -> str:
    """The rule that kept a Claude run off GLM (KEEPERS' names, else the raw reason). A run that stayed on
    Claude for reaching a person is split by how (`_person`), also where today's rules would send it to GLM."""
    from . import glmwork
    if run.get("role") == "coordinator":
        return "coordinator turn"
    if not task:
        return "unrecorded"
    kind = str(task.get("kind") or "")
    if kind in ("plan", "review"):
        return kind
    if kind not in glmwork.KINDS:
        return "kind off the list"
    if role_of(run, task, cfg, dev) == "device":
        return "device"
    note = _note(run.get("note"))
    arm = note.get("code_arm") if isinstance(note.get("code_arm"), dict) else {}
    ent = note.get(route.KEY) if isinstance(note.get(route.KEY), dict) else {}
    why = str(arm.get("why") or "").lower()
    if not why and ent.get("fallback"):
        why = {"escalated": str(ent.get("escalated") or "escalated"), "rerun": "rerun after a fallback"}.get(
            str(ent["fallback"]), "")
    if not why:   # the rule the daemon recorded when no route took the run (route.core_why)
        why = str(note.get(route.CORE_WHY) or "").lower()
    if m := re.match(r"lineage of #(\d+)", why):   # an older follow-up: the rule that put its root on Claude
        why = str((p.db.kv(f"{CODE_ARM_KV}{m.group(1)}") or {}).get("why") or why).lower()
    if not why and (p.db.kv(f"{ARM_KV}{task['id']}") or {}).get("arm") == "holdout":
        why = "holdout"
    if not why and "conclusion_floor" in note:
        why = "conclusion"
    if not why:
        try:
            why = glmwork.keeps_core(p.db, cfg, task, str((note.get("pick") or {}).get("tier") or task.get("tier") or "standard"),
                                     None, {"by": "retry"} if (note.get("pick") or {}).get("by") == "retry" else None).lower()
        except Exception:   # in doubt, unnamed
            why = ""
    if why.startswith("rerun"):
        return "rerun after a fallback"
    if (not why or why.startswith("person")) and (how := _person(task)):
        return how
    return next((name for key, name in _WHY if key in why), why.split(":")[0][:40] or "unrecorded")


def held(p, since: float, now: float) -> dict:
    """The window's Claude runs of eligible kinds, coordinator, plan and review runs by keeping rule (the module doc)."""
    cfg, db = p.config(), p.db
    rows = db.q("SELECT id, task, role, provider, model, started, ended, note FROM runs WHERE started>=? AND started<?",
                (since, now))
    tasks, dev, groups = _tasks(db, (r["task"] for r in rows)), {}, {}
    total = 0.0
    for r in rows:
        h = max(0.0, min(float(r["ended"] or now), now) - float(r["started"] or now)) / 3600
        total += h
        if on_glm(r["provider"], r["model"], r["note"]):
            continue
        name = keeper(p, cfg, r, tasks.get(r["task"]), dev)
        g = groups.setdefault(name, {"runs": 0, "hours": 0.0, "tasks": set(), "user_rule": KEEPERS.get(name, False)})
        g["runs"] += 1
        g["hours"] += h
        if r["task"]:
            g["tasks"].add(r["task"])
    out = {k: {"runs": v["runs"], "hours": round(v["hours"], 3), "tasks": len(v["tasks"]), "user_rule": v["user_rule"],
               "hour_pct": round(100.0 * v["hours"] / total, 1) if total else None} for k, v in groups.items()}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["hours"]))


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
    arms = {a: quality(db, cfg, ids, tasks, runs, *ctx, days, now, arm=a) for a, ids in by_arm.items()}
    usd = (db.one("SELECT SUM(usd) s FROM ledger WHERE provider='claude' AND ts>=? AND ts<?", (since, now)) or {}).get("s")
    from . import correction
    corr = [c for c in (db.kv(correction.KEY, []) or []) if isinstance(c, dict) and since <= float(c.get("ts") or 0) < now]
    return {"project": p.name, "since": since, "until": now, "days": days, "total": total, "roles": roles,
            "arms": arms, "probes": probe_arms(by_arm["glm"], tasks, ctx[0], probes_on_since(p)) if costlevers.lever_on(p, "glm_probes") else {}, "held": held(p, since, now), "claude_usd_per_day": round(float(usd or 0) / days, 2) if days > 0 else None,
            "corrections": {"n": len(corr), "per_arm": None,
                            "note": "a correction names no task, so it is counted per project; kept 7 days"},
            "bad_output": bad_output(p, rows),
            "reviews_on_glm": [{"run": r["id"], "started": r["started"], "class": _note(r["note"]).get(runclass.KEY) or "-"}
                               for r in rows if (tasks.get(r["task"]) or {}).get("kind") == "review"
                               and on_glm(r["provider"], r["model"], r["note"])]}


def _bridge_of(p, r) -> dict | None:
    """A run's exit-record bridge stats ({} when it had none), None when it has no exit record yet."""
    d = Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])
    try:
        got = json.loads((d / "exit.json").read_text())
    except (OSError, ValueError):
        return None
    b = got.get("bridge") if isinstance(got, dict) else None
    return b if isinstance(b, dict) else {}


def guard_fires(d: Path) -> dict:
    """glmguard's fires in one run folder: outward posts it drafted, the gate stop or other bounce it ended on, and
    the refusals it was redirected on and went on (glmguard.REFUSALS)."""
    from . import glmguard
    n = {k: 0 for k in FIRE_KEYS}
    for name in (glmguard.DRAFTS, glmguard.DRAFTS + ".sent"):
        try:
            n["outward_drafts"] += sum(1 for ln in (d / name).read_text().splitlines() if ln.strip())
        except OSError:
            pass
    try:
        hit = json.loads((d / glmguard.HIT).read_text())
    except (OSError, ValueError):
        hit = None
    if isinstance(hit, dict):
        n["gate_stops" if hit.get("kind") == glmguard.GATE else "guard_bounces"] += 1
    n["guard_redirects"] = sum(1 for r in glmguard.refusals(d) if not r.get("back"))
    return n


def bad_output(p, rows) -> list[dict]:
    """The GLM runs' bad output per home-zone day, oldest first (the module doc), with glmguard's fires
    (FIRE_KEYS), which count on every GLM run, measured by the bridge or not."""
    from . import timefmt
    tz, days = timefmt.zone(p), {}
    for r in rows:
        if not on_glm(r["provider"], r["model"], r["note"]):
            continue
        b = _bridge_of(p, r)
        fires = guard_fires(Path(r["dir"]) if r["dir"] else p.runs / str(r["id"]))
        scanned = (b or {}).get("usage", {}).get("scanned") if isinstance((b or {}).get("usage"), dict) else None
        day = datetime.datetime.fromtimestamp(float(r["started"] or 0), tz).strftime("%Y-%m-%d")
        if not isinstance(scanned, int):   # no exit record, or a bridge from before the counters: not measured
            if any(fires.values()):
                v = days.setdefault(day, _day(day))
                for k in FIRE_KEYS:
                    v[k] += fires[k]
            continue
        v = days.setdefault(day, _day(day))
        for k in FIRE_KEYS:
            v[k] += fires[k]
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


def _day(day: str) -> dict:
    return {"day": day, "runs": 0, "answers": 0, "runs_hit": 0, **{k: 0 for k in (*BAD_KEYS, *LOOP_KEYS, *FIRE_KEYS)}}


def bad_lines(got: list[dict]) -> list[str]:
    """One line per day of bad_output's list."""
    pct = lambda v: "-" if v is None else f"{v:g}%"   # noqa: E731
    return [f"{v['day']}: {v['runs']} GLM runs, {v['answers']} answers; tool input errors {v['tool_input_errors']} "
            f"({pct(v['pct']['tool_input_errors'])}), args unparsed {v['args_unparsed']} ({pct(v['pct']['args_unparsed'])}, "
            f"mended {v['args_repaired']}), "
            f"reasoning-only {v['empty_content']} ({pct(v['pct']['empty_content'])}), junk {v['junk_responses']} "
            f"({pct(v['pct']['junk_responses'])}); loop nudges {v.get('loop_nudges', 0)}, loop stops "
            f"{v.get('loop_stops', 0)}; runs hit {v['runs_hit']}; outward drafts {v.get('outward_drafts', 0)}, "
            f"gate stops {v.get('gate_stops', 0)}, guard bounces {v.get('guard_bounces', 0)}, "
            f"guard redirects {v.get('guard_redirects', 0)}" for v in got]


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
    if s.get("held"):
        lines.append("  held off GLM by rule (model-h, runs; * = the user's own rule):")
        lines += [f"    {k}{'*' if v['user_rule'] else ''}: {v['hours']:.2f} h ({pct(v['hour_pct'])} of the window), {v['runs']} runs"
                  for k, v in s["held"].items()]
    for a, q in s["arms"].items():
        lines.append(f"  {a}: {q['tasks']} tasks, {q['done']} done ({q['done_per_day'] or 0:g}/day), {q['failed']} failed, "
                     f"{q['reopened']} reopened, first review passed {pct(q['first_pass_pct'])} of {q['reviewed']}, "
                     f"fix rounds/reviewed {q['fix_rounds_per_reviewed'] if q['fix_rounds_per_reviewed'] is not None else '-'}, "
                     f"{q['bounces']} bounced, {q['incidents']} device incidents on GLM runs")
    for a, v in (s.get("probes") or {}).items():
        if v["reviewed"]:
            lines.append(f"  GLM code probes {a}: first review failed {pct(v['first_fail_pct'])} of {v['reviewed']}, "
                         f"{pct(v['logic_bug_pct'])} of the failures on a code finding")
    lines.append(f"  Claude ${s['claude_usd_per_day'] or 0:.2f}/day; user corrections {s['corrections']['n']} "
                 "(per project: a correction names no task)")
    if (rv := s.get("reviews_on_glm")):
        last = max(rv, key=lambda x: x["started"])
        lines.append(f"  reviews on GLM: {len(rv)}, newest {datetime.datetime.fromtimestamp(last['started']):%Y-%m-%d} "
                     f"(run class {last['class']})")
    if s.get("bad_output"):
        lines.append("  GLM bad output by day (rate per answer):")
        lines += [f"    {x}" for x in bad_lines(s["bad_output"])]
    return "\n".join(lines)


def settings(cfg: dict) -> dict:
    """The wave gate's sample minimums and margins: route.glm_work's own values, else the defaults (module doc)."""
    raw = ((cfg.get("route") or {}).get("glm_work")) if isinstance(cfg.get("route"), dict) else None
    raw = raw if isinstance(raw, dict) else {}
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0   # noqa: E731
    got = lambda k, d: type(d)(raw[k]) if num(raw.get(k)) else d   # noqa: E731
    return {"min_tasks": got("min_tasks", MIN_TASKS), "min_base": got("min_base", BASE_MIN),
            "min_window": got("min_window", BASE_WINDOW_MIN),
            "advance_pts": got("advance_pts", ADVANCE_PTS),
            "rollback_pts": got("rollback_pts", got("worse_pts", ROLLBACK_PTS)),
            "advance_rounds": got("advance_rounds", ADVANCE_ROUNDS),
            "rollback_rounds": got("rollback_rounds", ROLLBACK_ROUNDS),
            "advance_bounce_pts": got("advance_bounce_pts", ADVANCE_BOUNCE_PTS),
            "rollback_bounce_pts": got("rollback_bounce_pts", ROLLBACK_BOUNCE_PTS),
            "hold_done_pct": got("hold_done_pct", HOLD_DONE_PCT),
            "hold_corrections": got("hold_corrections", HOLD_CORRECTIONS)}


def _first_runs(db, lo: float, hi: float) -> set[int]:
    """Tasks whose first worker run started in [lo, hi)."""
    return {r["task"] for r in db.q("SELECT task, MIN(started) s FROM runs WHERE role='worker' AND task IS NOT NULL "
                                    "GROUP BY task HAVING s>=? AND s<?", (lo, hi))}


def _outcome_at(t: dict | None, rv: dict, rs: list[dict]) -> float | None:
    """When a task's outcome fell: its first auto review's created time, else (ended with no review open) the end
    of its last worker run, else its last update; None while it has none."""
    if rv.get("first"):
        return rv.get("at")
    if t and t["status"] in ("done", "failed") and not rv.get("open"):
        ends = [float(r["ended"]) for r in rs if r["ended"]]
        return max(ends) if ends else float(t.get("updated") or 0)
    return None


def base_window(cand, tasks: dict, runs: dict, reviews: dict, w0: float, floor: float, need: int) -> tuple:
    """(baseline task ids, window start): the Claude candidates whose outcome fell from w0 on, the GLM window's
    start; with fewer than `need` reviewed ones there, from the newest `need` reviewed outcomes' oldest (never
    before `floor`)."""
    at = {t: x for t in cand if (x := _outcome_at(tasks.get(t), reviews.get(t) or {}, runs.get(t) or [])) is not None}
    rev = sorted((at[t] for t in at if (reviews.get(t) or {}).get("first")), reverse=True)
    if sum(x >= w0 for x in rev) >= need:
        lo = w0
    else:
        lo = max(floor, rev[need - 1]) if len(rev) >= need else floor
    return [t for t in cand if t in at and at[t] >= lo], lo


def signals(g: dict, b: dict, before: dict, after: dict, corrections: int, st: dict) -> list[dict]:
    """Each wave-gate signal (module doc): the GLM arm `g` against the baseline `b`, the project's throughput
    `after` its first arm record against `before`, and the user corrections; verdict ok | hold | worse | unmeasured."""
    out = []
    for name, key, up, adv, back in GATED:
        gv, bv = g.get(key), b.get(key)
        few = key in REVIEW_KEYS and min(g["reviewed"], b["reviewed"]) < st["min_base"]
        if gv is None or bv is None or few:
            out.append({"signal": name, "glm": gv, "base": bv, "worse_by": None, "verdict": "unmeasured"})
            continue
        by = round((gv - bv) if up else (bv - gv), 2)
        out.append({"signal": name, "glm": gv, "base": bv, "worse_by": by, "advance": st[adv], "rollback": st[back],
                    "verdict": "worse" if by > st[back] else "hold" if by > st[adv] else "ok"})
    bd, ad = before.get("done_per_day"), after.get("done_per_day")
    if before["decided"] < st["min_base"] or not bd or ad is None:
        out.append({"signal": "tasks done per day", "glm": ad, "base": bd, "worse_by": None, "verdict": "unmeasured"})
    else:
        by = round(100.0 * (bd - ad) / bd, 1)
        out.append({"signal": "tasks done per day", "glm": ad, "base": bd, "worse_by": by, "advance": st["hold_done_pct"],
                    "verdict": "hold" if by > st["hold_done_pct"] else "ok"})
    out.append({"signal": "user corrections", "glm": corrections, "base": None, "worse_by": None,
                "advance": st["hold_corrections"],
                "verdict": "hold" if corrections > st["hold_corrections"] else "ok"})
    return out


def _rate_txt(v) -> str:
    """A rate for a why line: '-' when unmeasured."""
    return "-" if v is None else f"{v:g}"


def _says(s: dict) -> str:
    """One signal for a why line: GLM's value, the baseline's and the margin it is held to; a signal past a margin
    names that margin, a passing one says so plainly (never "worse by -20.6")."""
    if s["signal"] == "user corrections":
        return f"user corrections {s['glm']} in {CORR_DAYS} days (holds above {_rate_txt(s['advance'])})"
    head = f"{s['signal']} GLM {_rate_txt(s['glm'])} vs Claude {_rate_txt(s['base'])}"
    by, v = s["worse_by"], s["verdict"]
    if by is None:
        return f"{head} (too few outcomes to compare yet)"
    unit = "%" if s["signal"] == "tasks done per day" else ""
    if v in ("worse", "hold"):
        kind, margin = ("rollback", s.get("rollback")) if v == "worse" else ("advance", s["advance"])
        return f"{head}, worse by {_rate_txt(by)}{unit}, past the {kind} margin of {_rate_txt(margin)}{unit}"
    if not by:
        return f"{head}, level, within the margins"
    return f"{head}, {'better' if by < 0 else 'worse'} by {_rate_txt(abs(by))}{unit}, within the margins"


def _failing_first(sig: list[dict]) -> list[dict]:
    """The signals past a margin first (rollback, then advance), the rest in gate order."""
    rank = {"worse": 0, "hold": 1}
    return sorted(sig, key=lambda s: rank.get(s["verdict"], 2))


def judge(p, cfg: dict, now: float) -> dict:
    """{"state": "ok" | "worse" | "wait", "why", "n", "signals"}: the project's GLM arm against its own Claude
    baseline (the module doc); n is the GLM tasks with an outcome."""
    return _judge_arms(p, cfg, now, False)


def judge_code(p, cfg: dict, now: float) -> dict:
    """The glm_code flag's judge: the project's code-arm tasks (codearm's records) against its own Claude
    baseline on the metrics the flag owns: first review pass rate, bounce rate and tasks done per day. It waits
    where the GLM work route covers the project: that route decides its code tasks (codearm.settings) and
    glm_work's judge judges them."""
    from . import glmwork
    if glmwork.settings(glmwork.ring_gate(dict(cfg), getattr(p, "name", ""), p.raw_config())):
        return {"state": "wait", "why": "glm_work covers the code tasks here: its judge judges them", "n": 0,
                "signals": []}
    return _judge_arms(p, cfg, now, True)


def _judge_arms(p, cfg: dict, now: float, code: bool) -> dict:
    """The wave gate judge shared by glm_work and glm_code (`code`: the code arm's records, a baseline of code
    tasks and CODE_METRICS alone)."""
    from . import correction, deviceplan, rollout
    db, st = p.db, settings(cfg)
    recs = arm_records(db, CODE_ARM_KV if code else ARM_KV)
    if not recs:
        return {"state": "wait", "why": f"not enough data: no {'glm_code' if code else 'glm_work'} arm records yet",
                "n": 0, "signals": []}
    t0 = min(float(r.get("ts") or now) for r in recs.values())
    since = t0 - BASE_DAYS * 86400
    pre, post = _first_runs(db, since, t0), _first_runs(db, t0, now)
    ids = set(recs) | pre | post
    tasks, runs = _tasks(db, ids), _worker_runs(db, ids)
    work = lambda ts: [t for t in ts if (tasks.get(t) or {}).get("kind") != "review"]   # noqa: E731
    armed = rollout.armed_at(getattr(p, "name", ""), "glm_work", "glm_code")   # a re-arm starts a new window
    device = lambda t: deviceplan.device(tasks.get(t) or {}, cfg)   # noqa: E731
    glm = [t for t, r in recs.items() if r.get("arm") == "glm" and float(r.get("ts") or now) >= armed]
    dev_glm = [t for t in glm if device(t)]   # device runs are judged by glm_device's own judge: only incidents count here
    glm = [t for t in glm if t not in dev_glm]
    cand = [t for t, r in recs.items() if r.get("arm") != "glm" and not device(t)] + [
        t for t in work(pre | post) if t not in recs and runs.get(t) and not device(t)
        and (not code or (tasks.get(t) or {}).get("kind") == "code")
        and not on_glm(runs[t][0]["provider"], runs[t][0]["model"], runs[t][0]["note"])]
    ctx = (_reviews(db, since), _fix_rounds(db, since), _requeued(db, since))
    w0 = max(t0, armed)   # the GLM window being judged: the baseline is Claude's outcomes from the same period
    base, lo = base_window(cand, tasks, runs, ctx[0], w0, since, st["min_window"])
    days = max((now - t0) / 86400, 1e-9)
    fixed = rollout.fixed_stops(getattr(p, "name", ""), "glm_work", "glm_code")
    g = quality(db, cfg, glm, tasks, runs, *ctx, days, now, fixed)
    g["incidents"] += _incidents(db, cfg, [r for t in dev_glm for r in runs.get(t, [])
                                           if on_glm(r["provider"], r["model"], r["note"])], tasks, now)
    b = quality(db, cfg, base, tasks, runs, *ctx, days, now, arm="claude")
    before = quality(db, cfg, work(pre), tasks, runs, *ctx, BASE_DAYS, t0, arm="claude")
    after = quality(db, cfg, work(post), tasks, runs, *ctx, days, now, arm="claude")
    corr = sum(1 for c in (db.kv(correction.KEY, []) or [])
               if isinstance(c, dict) and now - CORR_DAYS * 86400 <= float(c.get("ts") or 0) < now)
    sig = signals(g, b, before, after, corr, st)
    if code:
        sig = [s for s in sig if s["signal"] in CODE_METRICS]
    n = g["decided"]
    frm = datetime.datetime.fromtimestamp(lo, datetime.timezone.utc)
    nums = (f"GLM {n} tasks with an outcome; Claude baseline {b['decided']} with an outcome since "
            f"{frm:%Y-%m-%d %H:%M} UTC; " + "; ".join(_says(s) for s in _failing_first(sig))
            + (f"; {rollout.not_counted_line(g['not_counted'], p)}" if g["not_counted"] else ""))
    out = lambda state, why: {"state": state, "why": f"{why}; {nums}", "n": n, "signals": sig,   # noqa: E731
                              "glm_pct": g["first_pass_pct"], "base_pct": b["first_pass_pct"],
                              "base_window": [round(lo, 3), round(now, 3)]}
    if g["incidents"]:
        return out("worse", f"{g['incidents']} device incident(s) on GLM runs")
    if n < st["min_tasks"] or b["decided"] < st["min_base"]:
        return out("wait", f"not enough data: {n} of {st['min_tasks']} GLM tasks and {b['decided']} of "
                           f"{st['min_base']} baseline tasks with an outcome")
    worse = [s["signal"] for s in sig if s["verdict"] == "worse"]
    if worse:
        return out("worse", f"worse than the Claude baseline past the rollback margin: {', '.join(worse)}")
    held = [s["signal"] for s in sig if s["verdict"] == "hold"]
    if held:
        return out("wait", f"holding the wave: {', '.join(held)} past the advance margin")
    return out("ok", "within every advance margin of the Claude baseline")
