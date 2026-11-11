"""`ttp stats --landing`: what each landed change cost in tests, CPU and dollars, per day in the home zone
(user msg #2220, #2221: one push train tested once; report test runs, CPU-hours and $ per landed change).

Per day: the changes the push queue landed (a queue entry pushed or landed, on its batch's end) and the tasks
that landed by a direct `ttp push` (task_landings, on their first landing; no approval wait), the task and
review `ttp checks` runs that ended (their checks.json: 'full' when a scope line ran a check in full or no
check was narrowed), the push trains that ran checks and their check runs, the CPU-hours the checks runs
recorded (cpu_s, from #2220 on; older runs recorded none), the train check hours (wall), the dollars every
run that started that day cost, and approval to landed (median, p90 by nearest rank) of the day's landings.
Dollars are billed dollars only: a plan-billed run's list price is `virtual_usd`, shown apart, and its
plan's window points are shown per landed change instead (planuse.py; user msg #140).

Tests are CPU work: no model spend is counted as test cost. What the checks do cost in model calls is apart
(checkwait): the worker and review runs that started while their task waited on its own `ttp checks` (the task's
previous run handed off with `retry_when` `ttp checks --result <dir>`), counted per day with their dollars and
split by why the run woke (its note's wake reason: timer, checks_failed, probe_passed, spec_change, ...).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from . import planuse, timefmt


def _pct(secs: list[float], q: float) -> float | None:
    if not secs:
        return None
    secs = sorted(secs)
    if q == 0.5:
        n = len(secs)
        return secs[n // 2] if n % 2 else (secs[n // 2 - 1] + secs[n // 2]) / 2
    return secs[max(0, -(-int(q * 10) * len(secs) // 10) - 1)]


def _full(rec: dict) -> bool:
    scope = [str(x) for x in rec.get("scope") or []]
    return not any(": targeted: " in x for x in scope) or any(": full: " in x for x in scope)


def report(db, runs_dir, tz: str, days: float = 7, now: float | None = None, cfg=None) -> dict:
    from .prguard import CHECKS_FILE
    now = time.time() if now is None else now
    since = now - days * 86400
    cls = planuse.Classifier(db, since, cfg)
    out: dict[str, dict] = {}

    def day(ts: float) -> dict:
        return out.setdefault(timefmt.today(ts, tz), {
            "landed": 0, "direct": 0, "checks_runs": 0, "full_runs": 0, "trains": 0, "train_check_runs": 0,
            "cpu_s": 0.0, "cpu_unrecorded": 0, "train_check_s": 0.0, "train_cpu_s": 0.0, "train_cpu_unrecorded": 0,
            "usd": 0.0, "virtual_usd": 0.0, "checkwait_wakes": 0, "checkwait_usd": 0.0,
            "checkwait_virtual_usd": 0.0, "checkwait_by_reason": {}, "_wait": []})

    for r in db.q("SELECT q.created, COALESCE(b.ended, q.updated) at_ FROM push_queue q LEFT JOIN push_batches b "
                  "ON b.id=q.batch WHERE q.status IN ('pushed','landed') AND COALESCE(b.ended, q.updated)>=?",
                  (since,)):
        d = day(float(r["at_"]))
        d["landed"] += 1
        d["_wait"].append(max(0.0, float(r["at_"]) - float(r["created"])))
    # A review that lands its change itself (`ttp push`, no push queue) leaves only a task_landings row:
    # count each such task once, on its first landing (a queue entry for the task already counted it).
    for r in db.q("SELECT task, min(ts) ts FROM task_landings WHERE ts>=? AND task NOT IN (SELECT task FROM "
                  "push_queue WHERE status IN ('pushed','landed')) GROUP BY task", (since,)):
        d = day(float(r["ts"]))
        d["landed"] += 1
        d["direct"] += 1
    for r in db.q("SELECT started, check_runs, check_s, check_cpu_s FROM push_batches WHERE started>=? AND check_runs>0",
                  (since,)):
        d = day(float(r["started"]))
        d["trains"] += 1
        d["train_check_runs"] += int(r["check_runs"] or 0)
        d["train_check_s"] += float(r["check_s"] or 0)
        # CPU of the train's checks run here (Batch._spent); a spilled run's host CPU is not in it
        if r["check_cpu_s"] is None:
            d["train_cpu_unrecorded"] += 1
        else:
            d["train_cpu_s"] += float(r["check_cpu_s"])
    for r in db.q("SELECT id, provider, account, started, ended, cost_usd FROM runs WHERE COALESCE(ended, started)>=?",
                  (since,)):
        if (r["started"] or 0) >= since:
            plan = cls.is_plan(r["provider"], r["account"], r["started"])
            day(float(r["started"]))["virtual_usd" if plan else "usd"] += float(r["cost_usd"] or 0)
        try:
            rec = json.loads((runs_dir / str(r["id"]) / CHECKS_FILE).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("purpose") not in (None, "candidate"):
            continue
        d = day(float(rec.get("ts") or r["ended"] or r["started"]))
        d["checks_runs"] += 1
        d["full_runs"] += _full(rec)
        if isinstance(rec.get("cpu_s"), (int, float)):
            d["cpu_s"] += float(rec["cpu_s"])
        else:
            d["cpu_unrecorded"] += 1
    for w in checkwait(db, since, now):
        d = day(w["started"])
        d["checkwait_wakes"] += 1
        d["checkwait_virtual_usd" if cls.is_plan(w["provider"], w["account"], w["started"]) else "checkwait_usd"] += w["usd"]
        n, usd = d["checkwait_by_reason"].get(w["reason"], (0, 0.0))
        d["checkwait_by_reason"][w["reason"]] = (n + 1, round(usd + w["usd"], 2))
    rows = []
    for k in sorted(out):
        d = out[k]
        wait, n = d.pop("_wait"), d["landed"]

        def per(v):
            return round(v / n, 2) if n else None
        rows.append({"day": k, **{x: (round(v, 2) if isinstance(v, float) else v) for x, v in d.items()},
                     "runs_per_landed": per(d["checks_runs"] + d["train_check_runs"]),
                     "full_per_landed": per(d["full_runs"] + d["trains"]),
                     "cpu_h_per_landed": per((d["cpu_s"] + d["train_cpu_s"]) / 3600),
                     "usd_per_landed": per(d["usd"]),
                     "checkwait_usd_per_landed": per(d["checkwait_usd"]),
                     "landing_median_h": round(m / 3600, 1) if (m := _pct(wait, 0.5)) is not None else None,
                     "landing_p90_h": round(m / 3600, 1) if (m := _pct(wait, 0.9)) is not None else None})
    return {"days": rows, "since": since, "until": now}


CHECKS_WAIT_RE = re.compile(r"\bchecks\s+--result\s+(\S+)")


def checkwait(db, since: float, until: float) -> list[dict]:
    """The worker and review runs started in [since, until) that woke while their task waited on its own checks:
    the task's run before them handed off with a `retry_when` naming `ttp checks --result <dir>`. Each row:
    run, task, started, provider, account, usd (its cost) and reason (its note's wake reason, 'unknown' if
    none). A task's first run in the window looks at its run just before the window."""
    rows = db.q("SELECT id, task, role, provider, account, started, cost_usd, note, dir FROM runs WHERE task IS NOT "
                "NULL AND role!='coordinator' AND task IN (SELECT DISTINCT task FROM runs WHERE started>=? AND "
                "started<?) AND started<? ORDER BY task, id", (since, until, until))
    out, prev = [], {}
    for r in rows:
        before, prev[r["task"]] = prev.get(r["task"]), r["dir"]
        if (r["started"] or 0) < since or not before:
            continue
        try:
            res = json.loads((Path(before) / "result.json").read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(res, dict) or not CHECKS_WAIT_RE.search(str(res.get("retry_when") or "")):
            continue
        try:
            reason = (json.loads(r["note"] or "{}").get("wake") or {}).get("reason") or "unknown"
        except (ValueError, AttributeError):
            reason = "unknown"
        out.append({"run": r["id"], "task": r["task"], "started": float(r["started"]), "provider": r["provider"],
                    "account": r["account"], "usd": float(r["cost_usd"] or 0), "reason": str(reason)})
    return out


BUMP_RE = re.compile(r"^tt-project: \d+\.\d+\.\d+ \(")   # the push queue's version-bump commit
WINDOWS = (("24 h", 86400.0), ("7 d", 7 * 86400.0))


def landed_commits(repo, ref: str, since: float, until: float) -> int | None:
    """Commits on `ref` committed in [since, until), merges and version bumps left out (None: git failed)."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", "--no-merges", f"--max-age={int(since)}",
                              "--format=%ct%x09%s", ref, "--"], capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode:
        return None
    n = 0
    for line in out.stdout.splitlines():
        ts, _, subject = line.partition("\t")
        if ts.isdigit() and since <= int(ts) < until and not BUMP_RE.match(subject):
            n += 1
    return n


def per_landed(db, repo, ref: str, now: float | None = None, cfg=None) -> list[dict]:
    """$ per landed change over the last 24 h and 7 d: the booked cost of the runs that started in the
    window over the commits landed on the push branch then (landed_commits), and the subagents those
    runs started (their run note's subagents_spawned, providers/claude.parse). Plan-billed runs' dollars
    are virtual: left out of `usd`, summed in `virtual_usd`, and `plan` gives each plan provider's window
    points per landed change (planuse.summary)."""
    now = time.time() if now is None else now
    rows = []
    cls = planuse.Classifier(db, now - max(s for _, s in WINDOWS), cfg)
    for label, secs in WINDOWS:
        since = now - secs
        usd, virtual, spawned, counted = 0.0, 0.0, 0, 0
        for r in db.q("SELECT provider, account, started, cost_usd, note FROM runs WHERE started>=? AND started<?",
                      (since, now)):
            if cls.is_plan(r["provider"], r["account"], r["started"]):
                virtual += float(r["cost_usd"] or 0)
            else:
                usd += float(r["cost_usd"] or 0)
            if "subagents_spawned" in (r["note"] or ""):
                try:
                    got = json.loads(r["note"]).get("subagents_spawned")
                except (ValueError, AttributeError):
                    continue
                if isinstance(got, int):
                    spawned, counted = spawned + got, counted + 1
        n = landed_commits(repo, ref, since, now) if repo and ref else None
        rows.append({"window": label, "usd": round(usd, 2), "landed_commits": n,
                     "usd_per_landed": round(usd / n, 2) if n else None,
                     "subagents_spawned": spawned, "runs_counting_spawns": counted,
                     "virtual_usd": round(virtual, 2),
                     "plan": planuse.summary(db, since, now, cfg, landed=n, cls=cls) if virtual else []})
    return rows


def per_landed_lines(rows: list[dict], ref: str = "") -> list[str]:
    return [f"{w['window']}: ${w['usd']:.2f} over {'?' if w['landed_commits'] is None else w['landed_commits']} "
            f"commits landed{f' on {ref}' if ref else ''} = "
            f"{'-' if w['usd_per_landed'] is None else '$' + format(w['usd_per_landed'], '.2f')} per landed change; "
            f"{w['subagents_spawned']} subagents spawned ({w['runs_counting_spawns']} runs counted them)"
            + (f"; plan-billed, not in the $: {planuse.text(w['plan'])}" if w.get("plan") else "")
            for w in rows]


def checkwait_reasons(by: dict) -> str:
    """'timer 12 $3.10, checks_failed 4 $1.20': wake reasons by count, most first."""
    return ", ".join(f"{k} {n} ${u:.2f}" for k, (n, u) in sorted(by.items(), key=lambda x: (-x[1][0], x[0])))


def text(r: dict, name: str) -> str:
    def f(v, unit=""):
        return "-" if v is None else f"{v}{unit}"
    lines = [f"{name}: per landed change, per day (home zone)"]
    for d in r["days"]:
        lines.append(
            f"  {d['day']}: {d['landed']} landed{f' ({x} by direct push)' if (x := d.get('direct')) else ''}; {d['checks_runs']} task checks runs ({d['full_runs']} full) + "
            f"{d['trains']} trains ({d['train_check_runs']} check runs): {f(d['runs_per_landed'])} runs and "
            f"{f(d['full_per_landed'])} full suites/landed; {f(d['cpu_h_per_landed'])} CPU-h/landed"
            + (f" ({d['cpu_unrecorded']} runs recorded no CPU)" if d["cpu_unrecorded"] else "")
            + f", train checks {d['train_check_s'] / 3600:.1f} h wall, {d['train_cpu_s'] / 3600:.1f} CPU-h"
            + (f" ({d['train_cpu_unrecorded']} trains recorded no CPU)" if d["train_cpu_unrecorded"] else "")
            + "; "
            f"all model spend ${f(d['usd_per_landed'])}/landed "
            f"(${d['usd']:.2f}{f' billed, ${v:.2f} virtual on a plan' if (v := d.get('virtual_usd')) else ''}); approved to landed median {f(d['landing_median_h'], ' h')}, "
            f"p90 {f(d['landing_p90_h'], ' h')}"
            + (f"; model spend while waiting on checks: {d['checkwait_wakes']} wakes, ${d['checkwait_usd']:.2f}"
               + (f" (+${v:.2f} virtual)" if (v := d.get('checkwait_virtual_usd')) else "")
               + f" ({checkwait_reasons(d['checkwait_by_reason'])})" if d.get("checkwait_wakes") else ""))
    if not r["days"]:
        lines.append("  nothing in the window")
    if r.get("per_landed"):
        lines.append("$ per landed change (booked spend over non-merge, non-bump commits on the push branch):")
        lines += [f"  {x}" for x in per_landed_lines(r["per_landed"], r.get("ref", ""))]
    return "\n".join(lines)
