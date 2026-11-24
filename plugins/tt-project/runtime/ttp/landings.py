"""Where landing time goes, from records the harness already keeps (`ttp landings`, the status and web
board's blocking step, the daily review's slowest-landings and recurring-cause lines). No model call.

For a task, the wall time from its creation to its landing (or to now, while it is open) is painted
interval by interval from: its worker runs, the checks slot waits and check runs of its tasks, its
review tasks, its push queue rows and their batches, its waiting, queue and lane events, its dependencies, its
start gate and a supersession recorded after it was done. Where two intervals overlap the more specific one wins
(PRIORITY); time nothing explains is `queue` before the first run and `unknown` after it.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

CAUSES = ("run", "checks run", "checks slot", "review", "push queue or batch", "busy resource", "dependency",
          "deferral", "own job", "retry wait", "ask or user", "late landing record", "queue", "unknown")
# Earlier wins on overlap: a worker run outranks everything, a measured wait outranks a marker.
PRIORITY = {c: i for i, c in enumerate(CAUSES)}
CLOSED = ("done", "failed", "cancelled")
DAY_S = 86400
EVENTS = ("task_waiting", "queue_wait", "checks_slot_wait", "urgent_check_wait", "ready_not_started",
          "lane_wait", "start_when_passed", "task_failed", "task_requeued", "task_blocked", "task_queued")
_WAITED = re.compile(r"waited (?:(\d+) h )?(\d+) min|ready (\d+) min")
# task_waiting's (wait, why) as the cause of the gap it opens
_WAIT_CAUSE = {("self", "checks"): "checks run", ("self", "push queue"): "push queue or batch",
               ("self", "detached jobs"): "own job", ("self", "hand-off"): "own job",
               ("self", "escalated wake"): "retry wait", ("external", "hand-off"): "deferral",
               ("external", "other"): "deferral"}
# the sums `ttp landings` shows per step; a step is one or more causes
STEPS = {"queue before the first run": None, "run": ("run",), "review": ("review",),
         "checks": ("checks run", "checks slot"), "push queue and batch": ("push queue or batch",)}


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _json(s) -> dict:
    try:
        got = json.loads(s) if isinstance(s, str) else s
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def _jsonl(path: Path, since: float, key: str) -> list[dict]:
    """The rows of a state jsonl (and its rotated `.1`) whose `key` time is at or after `since`."""
    out = []
    for f in (path.with_name(path.stem + ".1" + path.suffix), path):
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and (_f(r.get(key)) or 0) >= since:
                out.append(r)
    return out


def _minutes(text: str) -> float | None:
    m = _WAITED.search(text or "")
    if not m:
        return None
    return (int(m.group(1) or 0) * 60 + int(m.group(2))) * 60.0 if m.group(2) else int(m.group(3)) * 60.0


class Ctx:
    """The records of the tasks painted in one call, read once."""

    def __init__(self, db, tasks: list[dict], now: float, floor: float | None = None):
        self.db, self.now = db, now
        self.tasks = {t["id"]: t for t in tasks}
        ids = list(self.tasks)
        lo = min((_f(t["created"]) or now for t in tasks), default=now) if floor is None else floor
        self.state = Path(db.path).parent
        self.runs: dict[int, list[tuple[float, float, str]]] = {}
        self.events: dict[int, list[dict]] = {}
        self.pushes: dict[int, list[dict]] = {}
        self.batches: dict[str, dict] = {}
        self.others: dict[int, dict] = {}
        self.family: dict[int, list[int]] = {}   # the other tasks that carry a task's change on
        self.slots: dict[int, list[dict]] = {}
        self.checks: dict[int, list[dict]] = {}
        self.superseded: dict[int, list[float]] = {}
        if not ids:
            return
        try:
            from . import landed
            rows = landed.Rows(db, ids)
            self.family = {tid: sorted(set(landed.carriers(db, tid, rows)) - {tid}) for tid in ids}
        except Exception:   # a database the carriers cannot read
            self.family = {tid: [] for tid in ids}
        every = sorted(set(ids) | {d for t in tasks for d in self._deps(t)} | {m for f in self.family.values() for m in f})
        emark = ",".join("?" * len(every))
        self.others = {t["id"]: t for t in db.q(f"SELECT * FROM tasks WHERE id IN ({emark})", every)}
        for r in db.q(f"SELECT task, started, ended, status FROM runs WHERE task IN ({emark}) AND role!='coordinator' "
                      f"AND started IS NOT NULL ORDER BY started", every):
            self.runs.setdefault(r["task"], []).append(
                (float(r["started"]), float(r["ended"] or now), r["status"] or ""))
        for r in db.q(f"SELECT task, ts FROM task_superseded WHERE task IN ({emark})", every):
            self.superseded.setdefault(r["task"], []).append(float(r["ts"]))
        kids = ",".join("?" * len(EVENTS))
        for r in db.q(f"SELECT ts, kind, text, data, task FROM events WHERE task IN ({emark}) AND kind IN ({kids}) "
                      f"AND ts>=? ORDER BY ts", (*every, *EVENTS, lo - 1)):
            self.events.setdefault(r["task"], []).append(dict(r))
        try:
            for tid in ids:
                cm = ",".join("?" * (1 + len(self.family[tid])))
                self.pushes[tid] = [dict(r) for r in db.q(
                    f"SELECT status, batch, created, updated FROM push_queue WHERE task IN ({cm}) ORDER BY id",
                    [tid, *self.family[tid]])]
            batch_ids = sorted({r["batch"] for rs in self.pushes.values() for r in rs if r["batch"]})
            if batch_ids:
                self.batches = {b["id"]: dict(b) for b in db.q(
                    f"SELECT id, started, ended, check_s FROM push_batches WHERE id IN ({','.join('?' * len(batch_ids))})",
                    batch_ids)}
        except Exception:   # a database from before the push queue
            pass
        for r in _jsonl(self.state / "checks-slot-waits.jsonl", lo - 1, "queued_at"):
            if isinstance(r.get("task"), int):
                self.slots.setdefault(r["task"], []).append(r)
        for r in _jsonl(self.state / "checks-run-times.jsonl", lo - 1, "ts"):
            if isinstance(r.get("task"), int):
                self.checks.setdefault(r["task"], []).append(r)

    def members(self, t: dict) -> list[dict]:
        """The task and the tasks that carry its change on (fixes, rebases, reviews), as rows."""
        return [self.others[i] for i in (t["id"], *self.family.get(t["id"], [])) if i in self.others]

    @staticmethod
    def _deps(t: dict) -> list:
        from .db import dependency_ids
        return [d for d in dependency_ids(t) if isinstance(d, int)]

    def done_at(self, tid: int) -> float | None:
        """When a dependency finished its work: its last run's end, else its last update when it is closed."""
        rs = self.runs.get(tid) or []
        t = self.others.get(tid) or self.tasks.get(tid)
        if t and t["status"] in CLOSED:
            return max([r[1] for r in rs] or [_f(t["updated"]) or 0.0])
        return None


def intervals(ctx: Ctx, t: dict, end: float) -> list[tuple[float, float, str]]:
    """Every explained interval of task `t` up to `end`: (start, end, cause). The change's other tasks
    (fixes, rebases, reviews of it) count as `t`'s own: a review is its time from its creation to its first
    ok run's end, then it waits for its change to land (push queue or batch)."""
    tid, created = t["id"], _f(t["created"]) or 0.0
    out: list[tuple[float, float, str]] = []
    runs = ctx.runs.get(tid, [])
    first = runs[0][0] if runs else end
    starts = sorted(r[0] for m in ctx.members(t) for r in ctx.runs.get(m["id"], []))
    marks: list[tuple[float, str]] = []
    for m in ctx.members(t):
        mid, review = m["id"], m["kind"] == "review"
        rr = ctx.runs.get(mid, [])
        if review:
            ok = next((r for r in rr if r[2] == "ok"), None)
            a = _f(m["created"]) or created
            closed = m["status"] in CLOSED
            stop = (ok[1] if ok else (max([r[1] for r in rr] or [_f(m["updated"]) or end]) if closed else end))
            out.append((a, max(a, min(stop, end)), "review"))
            tail = (_f(m["updated"]) or end) if closed else end
            if ok and tail > ok[1]:
                out.append((ok[1], min(tail, end), "push queue or batch"))
        else:
            out += [(s, e, "run") for s, e, _st in rr]
        for r in ctx.slots.get(mid, []):
            a, b = _f(r.get("queued_at")), _f(r.get("granted_at"))
            if a and b and b > a:
                out.append((a, b, "checks slot"))
        for r in ctx.checks.get(mid, []):
            e, d = _f(r.get("ts")), _f(r.get("s"))
            if e and d and d > 0:
                out.append((e - d, e, "checks run"))
        for ev in ctx.events.get(mid, []):
            ts, k, data = float(ev["ts"]), ev["kind"], _json(ev.get("data"))
            if k == "task_waiting":
                c = _WAIT_CAUSE.get((data.get("wait"), data.get("why")),
                                    "own job" if data.get("wait") == "self" else "deferral")
                marks.append((ts, "review" if review and c == "own job" else c))
            elif k in ("task_failed", "task_requeued"):
                marks.append((ts, "retry wait"))
            elif k == "task_blocked":
                low = (ev["text"] or "").lower()
                marks.append((ts, "ask or user" if "ask" in low or "user" in low else "unknown"))
            elif k == "task_queued":
                marks.append((ts, "queue"))
            elif k == "lane_wait" and _f(data.get("waited_s")):
                out.append((ts - float(data["waited_s"]), ts, "busy resource"))
            elif k == "queue_wait":
                n = _minutes(ev["text"])
                ep = str(((data.get("must_act") or {}).get("episode")) or "")
                cause = "push queue or batch" if ":push" in ep else "checks slot" if ":checks" in ep else "busy resource"
                if n:
                    out.append((ts - n, ts, cause))
            elif k in ("checks_slot_wait", "urgent_check_wait"):
                if n := _minutes(ev["text"]):
                    out.append((ts - n, ts, "checks slot"))
            elif k == "ready_not_started":
                if n := _minutes(ev["text"]):
                    out.append((ts - n, ts, "queue"))
            elif k == "start_when_passed" and _f(data.get("since")):
                out.append((float(data["since"]), ts, "deferral"))
    for q in ctx.pushes.get(tid, []):
        a = _f(q["created"]) or created
        landed = q["status"] in ("pushed", "landed")
        b = (_f(q["updated"]) or end) if landed or q["status"] in ("dropped", "refused", "nothing") else end
        out.append((a, max(a, b), "push queue or batch"))
        bt = ctx.batches.get(q["batch"] or "")
        if bt and _f(bt.get("check_s")):
            s0 = _f(bt["started"]) or a
            out.append((s0, s0 + float(bt["check_s"]), "checks run"))
    for d in ctx._deps(t):
        done = ctx.done_at(d)
        out.append((created, min(done, end) if done else end, "dependency"))
    # a done task whose change was found on the push branch under another commit only later (task_superseded)
    done = ctx.done_at(tid)
    out += [(done, ts, "late landing record") for m in ctx.members(t) for ts in ctx.superseded.get(m["id"], [])
            if done and ts <= end]
    nb = _f(t.get("not_before"))
    if nb and created < nb <= first + 60:
        out.append((created, nb, "deferral"))
    marks.sort()
    for i, (ts, c) in enumerate(marks):
        nxt = [x for x in (*starts, *(m[0] for m in marks[i + 1:])) if x > ts]
        out.append((ts, min(nxt) if nxt else end, c))
    return out


def paint(ivs: list[tuple[float, float, str]], lo: float, hi: float, first_run: float | None) -> list[tuple[float, float, str]]:
    """The elementary segments of [lo, hi], each with its winning cause; unexplained time is `queue` before
    the first run and `unknown` after it."""
    ivs = [(max(a, lo), min(b, hi), c) for a, b, c in ivs if min(b, hi) > max(a, lo)]
    cuts = sorted({lo, hi, *(x for a, b, _ in ivs for x in (a, b))})
    out: list[tuple[float, float, str]] = []
    for a, b in zip(cuts, cuts[1:]):
        mid = (a + b) / 2
        got = [c for s, e, c in ivs if s <= mid < e]
        cause = min(got, key=PRIORITY.__getitem__) if got else (
            "queue" if first_run is None or mid < first_run else "unknown")
        if out and out[-1][2] == cause and out[-1][1] == a:
            out[-1] = (out[-1][0], b, cause)
        else:
            out.append((a, b, cause))
    return out


def breakdown(ctx: Ctx, t: dict, end: float) -> dict:
    """{"total_s", "causes": {cause: s}, "queue_first_s", "gap_s", "run_s", "runs", "segments"} of task `t`
    over [created, end]."""
    lo = _f(t["created"]) or 0.0
    runs = ctx.runs.get(t["id"], [])
    first = runs[0][0] if runs else None
    segs = paint(intervals(ctx, t, end), lo, max(end, lo), first)
    causes: dict[str, float] = {}
    for a, b, c in segs:
        causes[c] = causes.get(c, 0.0) + (b - a)
    before = sum(b - a for a, b, _ in segs if first is None or b <= first)
    run_s = causes.get("run", 0.0)
    return {"task": t["id"], "title": t["title"], "total_s": max(end - lo, 0.0), "causes": causes,
            "queue_first_s": before, "run_s": run_s, "gap_s": max(end - lo - run_s - before, 0.0),
            "runs": len(runs), "segments": segs}


def landed(db, since: float, now: float | None = None, kinds=("code",), p=None) -> list[dict]:
    """Breakdowns of the tasks of `kinds` whose change landed at or after `since`, oldest landing first."""
    from . import landed as lm
    now = time.time() if now is None else now
    cand = db.q(f"SELECT * FROM tasks WHERE kind IN ({','.join('?' * len(kinds))}) AND updated>=? ORDER BY id",
                (*kinds, since - 30 * DAY_S))
    when = {}
    rows = lm.Rows(db, [t["id"] for t in cand])
    when0 = lm.landed_whens(p, [t["id"] for t in cand], rows) if p is not None else {}
    for t in cand:
        try:
            at = when0.get(t["id"]) if p is not None else lm.landed_at(db, t["id"], rows)
        except Exception:
            at = None
        if at and since <= at <= now and at >= (_f(t["created"]) or 0):
            when[t["id"]] = at
    if not when:
        return []
    ts = [t for t in cand if t["id"] in when]
    ctx = Ctx(db, ts, now, floor=min(_f(t["created"]) or now for t in ts))
    rows = []
    for t in ts:
        b = breakdown(ctx, t, when[t["id"]])
        b["landed"] = when[t["id"]]
        rows.append(b)
    return sorted(rows, key=lambda r: r["landed"])


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[int(round(q * (len(xs) - 1)))] if xs else 0.0


def summary(rows: list[dict], top: int = 5) -> dict:
    """Totals, shares and percentiles over `landed()` rows."""
    tot = [r["total_s"] for r in rows]
    n = len(rows)
    causes: dict[str, float] = {}
    gaps: dict[str, float] = {}
    for r in rows:
        first = r["landed"] - r["total_s"] + r["queue_first_s"]
        for c, s in r["causes"].items():
            causes[c] = causes.get(c, 0.0) + s
        for a, b, c in r["segments"]:
            if a >= first - 1e-6 and c != "run":
                gaps[c] = gaps.get(c, 0.0) + (b - a)
    total = sum(tot)
    steps = {}
    for name, cs in STEPS.items():
        s = sum(r["queue_first_s"] for r in rows) if cs is None else sum(causes.get(c, 0.0) for c in cs)
        steps[name] = {"s": s, "share": s / total if total else 0.0}
    gap_total = sum(r["gap_s"] for r in rows)
    slow = sorted(rows, key=lambda r: -r["total_s"])[:top]
    return {"count": n, "total_s": total, "median_s": _pct(tot, 0.5), "p75_s": _pct(tot, 0.75),
            "p90_s": _pct(tot, 0.9), "mean_s": total / n if n else 0.0,
            "steps": steps, "causes": {c: {"s": s, "share": s / total if total else 0.0}
                                       for c, s in sorted(causes.items(), key=lambda x: -x[1])},
            "gaps": {"total_s": gap_total, "by_cause": dict(sorted(gaps.items(), key=lambda x: -x[1]))},
            "runs_per_task": {"mean": sum(r["runs"] for r in rows) / n if n else 0.0,
                              "three_or_more": sum(r["runs"] >= 3 for r in rows)},
            "slowest": [{"task": r["task"], "title": r["title"][:90], "total_s": r["total_s"],
                         "dominant": dominant(r), "runs": r["runs"]} for r in slow]}


def dominant(b: dict) -> str:
    """The cause that took most of a task's time, run time left out when anything else took a third of it."""
    cs = {c: s for c, s in b["causes"].items() if c != "run"}
    c, s = max(cs.items(), key=lambda x: x[1], default=("run", 0.0))
    return c if s >= b["total_s"] / 3 or not b["causes"].get("run") else "run"


def dur(s: float) -> str:
    s = float(s)
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min" if s >= 60 else f"{s:.0f} s"


def text(sm: dict, days: float) -> str:
    """The `ttp landings` table."""
    if not sm["count"]:
        return f"no change landed in the last {days:g} days"
    lines = [f"{sm['count']} landed in {days:g} days: created to landed median {dur(sm['median_s'])}, "
             f"p75 {dur(sm['p75_s'])}, p90 {dur(sm['p90_s'])}, mean {dur(sm['mean_s'])}",
             "step (all landings): " + "; ".join(f"{k} {dur(v['s'])} ({v['share']:.0%})" for k, v in sm["steps"].items()),
             "cause (all landings): " + "; ".join(f"{k} {dur(v['s'])} ({v['share']:.0%})" for k, v in sm["causes"].items()),
             f"gaps between runs {dur(sm['gaps']['total_s'])}: "
             + "; ".join(f"{k} {dur(v)}" for k, v in sm["gaps"]["by_cause"].items()),
             f"runs per landed task {sm['runs_per_task']['mean']:.1f}, {sm['runs_per_task']['three_or_more']} took 3 or more"]
    lines += [f"slowest: #{x['task']} {dur(x['total_s'])}, mostly {x['dominant']}, {x['runs']} runs: {x['title']}"
              for x in sm["slowest"]]
    return "\n".join(lines)


def open_tasks(db, now: float, limit: int = 60) -> list[dict]:
    return db.q(f"SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled') ORDER BY priority, id LIMIT ?",
                (limit,))


def blocking(db, tasks: list[dict] | None = None, now: float | None = None) -> dict[int, dict]:
    """{task id: {"step", "since", "waited_s", "total_s"}} for each open task: the cause its live time is in
    now and how long that cause has lasted (the contiguous painted segments of it), and its age."""
    now = time.time() if now is None else now
    tasks = open_tasks(db, now) if tasks is None else [t for t in tasks if t["status"] not in CLOSED]
    if not tasks:
        return {}
    ctx = Ctx(db, tasks, now)
    out = {}
    for t in tasks:
        segs = breakdown(ctx, t, now)["segments"]
        if not segs:
            continue
        step, start = segs[-1][2], segs[-1][0]
        if t["status"] == "blocked" and step in ("unknown", "queue"):
            low = (t.get("blocked_reason") or "").lower()
            step = "ask or user" if "ask" in low or "user" in low else "unknown"
        out[t["id"]] = {"step": step, "since": start, "waited_s": max(now - start, 0.0),
                        "total_s": max(now - (_f(t["created"]) or now), 0.0)}
    return out


def blocking_words(b: dict) -> str:
    """`blocked in checks slot for 12 min` (a run is `running`)."""
    return f"running for {dur(b['waited_s'])}" if b["step"] == "run" else f"in {b['step']} for {dur(b['waited_s'])}"


def push_batch_deaths(db, since: float) -> dict:
    """push_batch_died events since `since`: the count and the causes their texts give (reboot, kill, ...)."""
    rows = db.q("SELECT text FROM events WHERE kind='push_batch_died' AND ts>=?", (since,))
    causes: dict[str, int] = {}
    for r in rows:
        t = (r["text"] or "").lower()
        c = ("no outcome written" if "before writing an outcome" in t else "reboot" if "reboot" in t else "kill" if "kill" in t else "crash" if "crash" in t
             else "timeout" if "timed out" in t or "timeout" in t else "no outcome written")
        causes[c] = causes.get(c, 0) + 1
    return {"count": len(rows), "causes": dict(sorted(causes.items(), key=lambda x: -x[1]))}


def event_counts(db, since: float) -> dict[str, int]:
    """The wait events of the window by kind."""
    return {r["kind"]: r["n"] for r in db.q(
        f"SELECT kind, COUNT(*) n FROM events WHERE ts>=? AND kind IN ({','.join('?' * (len(EVENTS) + 3))}) GROUP BY kind",
        (since, *EVENTS, "push_batch_died", "checks_relaunched", "checks_flaky_test"))}


# the daily review -----------------------------------------------------------------------------------
FIX_TITLE = "landing time: "


def fix_open(db, cause: str) -> bool:
    """Whether a task about `cause` is still open (the review adds one only when none is)."""
    return bool(db.q("SELECT 1 FROM tasks WHERE status NOT IN ('done','failed','cancelled') AND title LIKE ? LIMIT 1",
                     (f"{FIX_TITLE}{cause}%",)))


def audit_lines(db, now: float, top: int = 5, p=None) -> list[str]:
    """The daily review's lines: the slowest landings of 24 h with their dominant cause, and the top
    recurring cause over 7 days with the instruction to queue one fix task for it (never a second while
    one is open)."""
    day = landed(db, now - DAY_S, now, p=p)
    out = []
    if day:
        slow = sorted(day, key=lambda r: -r["total_s"])[:top]
        out.append(f"slowest landings, 24 h ({len(day)} landed): " + "; ".join(
            f"#{r['task']} {dur(r['total_s'])} mostly {dominant(r)}" for r in slow))
    week = landed(db, now - 7 * DAY_S, now, p=p)
    if week:
        sm = summary(week)
        pool = {c: s for c, s in sm["gaps"]["by_cause"].items() if c not in ("run", "queue")}
        if pool:
            cause, s = next(iter(pool.items()))
            share = s / sm["total_s"] if sm["total_s"] else 0.0
            line = (f"top recurring landing delay, 7 d: {cause} {dur(s)} ({share:.0%} of {sm['count']} landings' "
                    f"time; median created to landed {dur(sm['median_s'])}, p90 {dur(sm['p90_s'])})")
            line += (f"; a task for it is open" if fix_open(db, cause) else
                     f"; no task for it is open: queue one fix task titled '{FIX_TITLE}{cause}', at most one")
            out.append(line)
    return out
