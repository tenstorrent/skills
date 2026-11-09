"""`ttp stats --landing`: what each landed change cost in tests, CPU and dollars, per day in the home zone
(user msg #2220, #2221: one push train tested once; report test runs, CPU-hours and $ per landed change).

Per day: the changes the push queue landed (a queue entry pushed or landed, on its batch's end), the task and
review `ttp checks` runs that ended (their checks.json: 'full' when a scope line ran a check in full or no
check was narrowed), the push trains that ran checks and their check runs, the CPU-hours the checks runs
recorded (cpu_s, from #2220 on; older runs recorded none), the train check hours (wall), the dollars every
run that started that day cost, and approval to landed (median, p90 by nearest rank) of the day's landings.
"""
from __future__ import annotations

import json
import time

from . import timefmt


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


def report(db, runs_dir, tz: str, days: float = 7, now: float | None = None) -> dict:
    from .prguard import CHECKS_FILE
    now = time.time() if now is None else now
    since = now - days * 86400
    out: dict[str, dict] = {}

    def day(ts: float) -> dict:
        return out.setdefault(timefmt.today(ts, tz), {
            "landed": 0, "checks_runs": 0, "full_runs": 0, "trains": 0, "train_check_runs": 0,
            "cpu_s": 0.0, "cpu_unrecorded": 0, "train_check_s": 0.0, "usd": 0.0, "_wait": []})

    for r in db.q("SELECT q.created, COALESCE(b.ended, q.updated) at_ FROM push_queue q LEFT JOIN push_batches b "
                  "ON b.id=q.batch WHERE q.status IN ('pushed','landed') AND COALESCE(b.ended, q.updated)>=?",
                  (since,)):
        d = day(float(r["at_"]))
        d["landed"] += 1
        d["_wait"].append(max(0.0, float(r["at_"]) - float(r["created"])))
    for r in db.q("SELECT started, check_runs, check_s FROM push_batches WHERE started>=? AND check_runs>0", (since,)):
        d = day(float(r["started"]))
        d["trains"] += 1
        d["train_check_runs"] += int(r["check_runs"] or 0)
        d["train_check_s"] += float(r["check_s"] or 0)
    for r in db.q("SELECT id, started, ended, cost_usd FROM runs WHERE COALESCE(ended, started)>=?", (since,)):
        if (r["started"] or 0) >= since:
            day(float(r["started"]))["usd"] += float(r["cost_usd"] or 0)
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
    rows = []
    for k in sorted(out):
        d = out[k]
        wait, n = d.pop("_wait"), d["landed"]

        def per(v):
            return round(v / n, 2) if n else None
        rows.append({"day": k, **{x: (round(v, 2) if isinstance(v, float) else v) for x, v in d.items()},
                     "runs_per_landed": per(d["checks_runs"] + d["train_check_runs"]),
                     "full_per_landed": per(d["full_runs"] + d["trains"]),
                     "cpu_h_per_landed": per(d["cpu_s"] / 3600),
                     "usd_per_landed": per(d["usd"]),
                     "landing_median_h": round(m / 3600, 1) if (m := _pct(wait, 0.5)) is not None else None,
                     "landing_p90_h": round(m / 3600, 1) if (m := _pct(wait, 0.9)) is not None else None})
    return {"days": rows, "since": since, "until": now}


def text(r: dict, name: str) -> str:
    def f(v, unit=""):
        return "-" if v is None else f"{v}{unit}"
    lines = [f"{name}: per landed change, per day (home zone)"]
    for d in r["days"]:
        lines.append(
            f"  {d['day']}: {d['landed']} landed; {d['checks_runs']} task checks runs ({d['full_runs']} full) + "
            f"{d['trains']} trains ({d['train_check_runs']} check runs): {f(d['runs_per_landed'])} runs and "
            f"{f(d['full_per_landed'])} full suites/landed; {f(d['cpu_h_per_landed'])} CPU-h/landed"
            + (f" ({d['cpu_unrecorded']} runs recorded no CPU)" if d["cpu_unrecorded"] else "")
            + f", train checks {d['train_check_s'] / 3600:.1f} h wall; ${f(d['usd_per_landed'])}/landed "
            f"(${d['usd']:.2f}); approved to landed median {f(d['landing_median_h'], ' h')}, "
            f"p90 {f(d['landing_p90_h'], ' h')}")
    if not r["days"]:
        lines.append("  nothing in the window")
    return "\n".join(lines)
