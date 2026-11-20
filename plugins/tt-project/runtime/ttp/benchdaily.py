"""The daily model bench: every project on this host, each day's landed code tasks, replayed from the
task's real starting point by Sonnet, Opus and an open model, graded by a blind Opus review.

`ttp bench daily` picks the day's landed code tasks from every project `ttp list` shows here (their
records read-only, through a read-only connection), leaves out device tasks, runs each remaining task
as one cell per arm (benchrun.run_cell: same isolation and time limit as the live-ab bench), has a
blind reviewer score the diffs (benchblind), and adds the day to a running table per project and per
model. Claude spend (arm runs and reviews) stops at a daily cap, counted from a running total in
code; a task the cap leaves out is recorded as skipped. Another project's repository is never
written: a bench-owned mirror clone of it (under the bench folder) is all the cells and the pick read.
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import bench, benchblind, benchfix, benchnotrun, benchpack, benchrun, benchv2, blindreview, effort, gitmemo, serverhealth
from .project import durable_write, open_append

CAP_USD = 0.0        # Claude spend a day (arm runs plus reviews); 0: no cap (the runaway guard is separate)
GUARD_USD = 600.0    # the runaway guard: a stop far above any sensible day, so a runaway run is cut off
CELL_USD = 5.0       # most one arm run may spend (the run's own budget stop)
REVIEW_USD = 2.0     # most one blind review call may spend
FLOOR_USD = 1.0      # least a measured reservation holds for one task's arm runs
REVIEW_FLOOR_USD = 0.25  # least a measured reservation holds for one task's review
MEASURED_AFTER = 2   # tasks finished before reservations follow measured spend (until then the worst case)
MEASURED_X = 2.0     # a measured reservation is this many times the running mean
MAX_LINES = 800      # changed non-test lines at most: a larger change does not fit one 60-minute cell
LIMIT_S = 3600       # a cell's time limit; reaching it is a fail
LANDED_MIN = 3       # requirements and correctness each at least this, on the 0-4 scale, count as landed
PARALLEL = 3         # tasks run at once
ARMS = ("sonnet=claude:sonnet:high", "opus=claude:opus:high")
GLM_ARM = "glm=chat:zai-org/GLM-5.3:max"
GLM_HARNESS = "mini-swe-agent"   # the open-model arm's default agent: the winner of the harness A/B (bridge: 'claude')
REVIEWER = ("claude", "opus", "high")
LOOKBACK_DAYS = 21   # a task created this long before the day may still land on it
TZ = "America/Los_Angeles"


def home() -> Path:
    from . import modelbench
    return modelbench.home() / "daily"


def window(day: str | None, now: float | None = None, tz: str = TZ) -> tuple[str, float, float]:
    """(day, start, end) in epoch seconds of `day` (YYYY-MM-DD) in zone `tz`; no day: yesterday there."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(tz)
    if day:
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone)
    else:
        n = datetime.fromtimestamp(now if now is not None else time.time(), zone)
        d = (n - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = (d.replace(hour=12) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.strftime("%Y-%m-%d"), d.timestamp(), nxt.timestamp()


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "project"


# -- projects: read-only

def projects(skip: str | None = None) -> list[dict]:
    """The projects on this host: {name, db, root} (the project's working repository, where its folder lives).
    Their databases are only ever opened read-only."""
    from . import globalcap
    out = []
    for name, db in globalcap.local_projects(skip):
        out.append({"name": name, "db": Path(db), "root": Path(db).parent.parent.parent})
    return out


def open_ro(path: Path):
    from . import overseer
    return overseer._ro_db(path)


def repo_of(proj: dict, repos: dict | None = None) -> Path | None:
    """The git repository the project's code landed in: the one this host's config names for it (`repos`:
    project name -> path), else the project root when that is a repository."""
    path = Path(str((repos or {}).get(proj["name"]) or proj["root"])).expanduser()
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--git-dir"], capture_output=True, text=True)
    return path if r.returncode == 0 and path.is_dir() else None


def _git(repo: Path, *args: str, timeout: int = 120, inp: str | None = None) -> str:
    r = gitmemo.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout, input=inp)
    if r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def mirror(proj: dict, base: Path | None = None, source: Path | None = None) -> Path:
    """A bench-owned bare mirror of the project's repository. It copies no objects: it reads the source's object
    store in place through objects/info/alternates (read-only), and holds only a copy of the source's branch
    refs, which the bench needs to find a task's branch. Made once, refreshed by rewriting those refs; every
    write happens inside the mirror: the source repository, its .git and its state are only read."""
    dest = (base or home() / "mirrors") / slug(proj["name"])
    src = Path(source or proj["root"])
    try:
        objects = _git(src, "rev-parse", "--path-format=absolute", "--git-path", "objects").strip()
        heads = dict(reversed(l.split(" ", 1)) for l in _git(src, "for-each-ref", "--format=%(objectname) %(refname)",
                                                             "refs/heads").splitlines())
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            _git(dest.parent, "init", "-q", "--bare", str(dest))
        info = dest / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        if not (info / "alternates").exists() or (info / "alternates").read_text().strip() != objects:
            durable_write(info / "alternates", objects + "\n")
        have = {l.split(" ", 1)[1]: l.split(" ", 1)[0] for l in _git(dest, "for-each-ref", "--format=%(objectname) %(refname)",
                                                                     "refs/heads").splitlines()}
        cmds = [f"delete {ref}\n" for ref in have if ref not in heads]
        cmds += [f"update {ref} {sha}\n" for ref, sha in heads.items() if have.get(ref) != sha]
        if cmds:
            _git(dest, "update-ref", "--stdin", inp="".join(cmds))
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"{proj['name']}: mirror {'refresh' if dest.exists() else 'preparation'} failed: "
                           f"{str(e).strip()[:300]}") from e
    return dest


# -- pick

def landed_at(db, repo: Path | None, rows: list[dict]) -> float | None:
    """When a task's landing finished: the latest of its landings' push queue row update times, else its
    landed commit's time (None when that needs a repository and there is none yet)."""
    times = []
    for sha in dict.fromkeys(r["landed_sha"] for r in rows):
        got = db.q("SELECT MAX(updated) AS t FROM push_queue WHERE COALESCE(NULLIF(landed_sha, ''), pushed_sha)=?", (sha,))
        if got and got[0]["t"]:
            times.append(float(got[0]["t"]))
        elif repo is None:
            return None
        else:
            t = bench._out(repo, "log", "-1", "--format=%ct", sha)
            times.append(float(t) if t else 0.0)
    return max(times)


def landed_in_window(db, start: float, end: float, lookback_days: int = LOOKBACK_DAYS) -> int:
    """How many code tasks may have landed in [start, end), read from the records alone (no repository):
    those landed on the day by the push queue's own times, and those whose landing time needs the repository.
    0 means the project has nothing to mirror for the day."""
    n = 0
    with gitmemo.scope({}):
        tasks, rows, pushed, reach, stops, shared = bench.candidates(db, start - lookback_days * 86400)
        for t in tasks:
            if t["kind"] != "code":
                continue
            mine = bench.landed_rows(db, t["id"], rows, pushed, reach)
            if mine and ((at := landed_at(db, None, mine)) is None or start <= at < end):
                n += 1
    return n


def is_device(task) -> bool:
    return effort.device_task(dict(task))


def pick(db, repo: Path, start: float, end: float, max_lines: int = MAX_LINES,
         lookback_days: int = LOOKBACK_DAYS) -> tuple[list[dict], list[dict]]:
    """(kept, dropped) for one project: the code tasks that landed in [start, end). A kept task is
    {task, kind, title, spec, base, head, tree is filled by the caller, parent, changed_lines, stratum, landed};
    a dropped one is {task, why}. The base is the task's real starting point: where its change starts on the
    push branch, or the head of the unlanded branch it continued. Device tasks are dropped (a device is not
    here), as are changes whose spec quotes the fix, that touch no non-test file or are too large."""
    kept, dropped = [], []
    with gitmemo.scope({}):
        tasks, rows, pushed, reach, stops, shared = bench.candidates(db, start - lookback_days * 86400)
        for t in tasks:
            if t["kind"] != "code":
                continue
            mine = bench.landed_rows(db, t["id"], rows, pushed, reach)
            if not mine:
                continue    # not landed (or not by a push this bench can read): not a candidate at all
            if not (start <= (landed_at(db, repo, mine) or 0) < end):
                continue    # landed on another day
            if is_device(t):
                dropped.append({"task": t["id"], "why": "needs a device"})
                continue
            base, head, why = bench.own_range(repo, mine, stops, shared)
            parent = bench.parent_of(t)
            if not why and parent:
                base, head, why = bench.from_parent(db, repo, parent[0], parent[1], mine[0]["head"] or head)
            added, lines = bench._changed(repo, base, head) if not why else ([], 0)
            if not why and not lines:
                why = "its landed change touches no non-test file"
            if not why and lines > max_lines:
                why = f"its landed change is {lines} lines, over the {max_lines} a cell can take"
            if not why and (n := bench.quotes(t["spec"] or "", added)) >= bench.QUOTE_LINES:
                why = f"its spec quotes {n} lines of the fix"
            if why:
                dropped.append({"task": t["id"], "why": why})
                continue
            kept.append({"task": t["id"], "kind": t["kind"], "title": t["title"], "spec": t["spec"] or "",
                         "base": base, "head": head, "parent": parent[0] if parent else None,
                         "changed_lines": lines, "stratum": "small" if lines <= bench.SMALL else "large"})
    return kept, dropped


def sample(by_project: dict[str, list[dict]], seed: int) -> list[dict]:
    """Every project's kept tasks in one order that a cap cuts fairly: each project's tasks shuffled by
    `seed`, then one from each project in turn, so a busy project does not crowd the others out."""
    rng = random.Random(seed)
    lists = {p: rng.sample(ts, len(ts)) for p, ts in sorted(by_project.items())}
    out = []
    while any(lists.values()):
        for p in sorted(lists):
            if lists[p]:
                out.append(lists[p].pop(0))
    return out


# -- spend

class Ledger:
    """Claude spend counted from a running total. A task is admitted only while spent + what the tasks in
    flight and the reviews still to run may spend + this task's reservation fits the cap; a cap of 0 means
    no cap: every task is admitted. A reservation is the worst case (every run spends its whole budget)
    until MEASURED_AFTER tasks have finished; then it is MEASURED_X times the mean actual spend of the
    finished ones (at least the floor, at most the worst case), so the cap buys the tasks it can really pay
    for. Actual spend still never passes the cap: a run's own budget stop bounds each cell."""

    def __init__(self, cap: float, spent: float = 0.0, review_mean: float | None = None):
        self.cap, self.spent, self.flight, self.reviews = cap, spent, 0.0, 0.0
        self.uncapped = not self.cap    # a missing or 0 cap: no task is left out for spend
        self.review_mean = review_mean    # a measured review spend per task (an earlier day's), or None
        self.finished: list[float] = []   # actual arm spend of each task finished in this run
        self.lock = threading.Lock()

    def reservation(self, worst: float, review: float) -> tuple[float, float, str]:
        """(arms, review, rule) this task would reserve now."""
        with self.lock:
            if len(self.finished) >= MEASURED_AFTER:
                mean = sum(self.finished) / len(self.finished)
                arms = min(worst, max(FLOOR_USD, MEASURED_X * mean))
                rule = f"measured: {MEASURED_X:g} x ${mean:.2f} mean of {len(self.finished)} finished"
            else:
                arms, rule = worst, "worst case: fewer than %d tasks finished" % MEASURED_AFTER
            if self.review_mean is not None:
                review = min(review, max(REVIEW_FLOOR_USD, MEASURED_X * self.review_mean))
            return arms, review, rule

    def admit(self, worst: float, review: float) -> bool:
        with self.lock:
            if not self.uncapped and self.spent + self.flight + self.reviews + worst + review > self.cap:
                return False
            self.flight += worst
            self.reviews += review
            return True

    def done(self, worst: float, actual: float) -> None:
        with self.lock:
            self.flight = max(0.0, self.flight - worst)
            self.spent += actual
            self.finished.append(actual)

    def reviewed(self, reserved: float, actual: float) -> None:
        with self.lock:
            self.reviews = max(0.0, self.reviews - reserved)
            self.spent += actual

    def total(self) -> float:
        with self.lock:
            return round(self.spent, 4)


def is_claude(arm: dict) -> bool:
    return arm.get("provider") == "claude"


def cells_limits(cfg: dict) -> dict:
    """How many cells run at once per provider: bench.cells.claude and bench.cells.glm (0 or missing: the
    PARALLEL default, one number for every provider as before)."""
    cells = (cfg.get("bench") or {}).get("cells") or {}
    got = {"claude": int(cells.get("claude") or 0), "chat": int(cells.get("glm") or 0)}
    return {p: max(1, n) if n else PARALLEL for p, n in got.items()}


def guard_usd(cfg: dict, ordered_run: bool = False) -> float | None:
    """The runaway guard's dollar stop, or None when it is off. It bounds a run whose spend runs away
    (config bench.daily.guard_usd; 0 disables it). An ordered run (an explicit task order from the
    coordinator or the user) never stops for it: its tasks were explicitly asked for."""
    if ordered_run:
        return None
    cfg_guard = ((cfg.get("bench") or {}).get("daily") or {}).get("guard_usd", GUARD_USD)
    return float(cfg_guard) if cfg_guard else None


# -- run

def build(day: str, proj: dict, kept: list[dict], repo: Path, bench_dir: Path) -> dict[str, dict]:
    """Each kept task's clean workspace under bench_dir (history cut at its base), keyed by its bench id."""
    tasks = {}
    with gitmemo.scope({}):
        for k in kept:
            tid = f"{slug(proj['name'])}-{k['task']}"
            here = bench_dir / tid
            if not (here / "workspace").exists():
                tree = bench.workspace(repo, k["base"], here / "workspace")
            else:
                tree = bench._out(here / "workspace", "rev-parse", "HEAD^{tree}")
            tasks[tid] = {**k, "task": tid, "project": proj["name"], "source_task": k["task"], "tree": tree,
                          "tests": [], "heldout": {}, "repo": str(repo), "source_root": str(proj["root"]),
                          "state": str(proj["db"].parent),
                          "spec": bench.scrub_spec(k["spec"], [repo, repo.resolve(), proj["root"]])}
    return tasks


def read_rows(path: Path) -> dict[tuple, dict]:
    """The last row of each cell (serverhealth.latest: void server marks skipped)."""
    return serverhealth.latest(benchrun.json_lines(path))


def append_row(path: Path, row: dict, lock: threading.Lock) -> None:
    with lock, open_append(path, "ab+") as f:
        torn = f.seek(0, 2) > 0 and f.seek(-1, 2) >= 0 and f.read(1) != b"\n"
        f.write((b"\n" if torn else b"") + json.dumps(row).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())


def run_task(out: Path, bench_dir: Path, task: dict, arms: dict, opts: dict, cfg: dict, ledger: Ledger,
             lock: threading.Lock) -> list[dict]:
    """The arms of one task, run one after the other (so a paired cell is never half run at the cap); each
    row goes to results.jsonl as it ends. A cell that already has a row is not run again. A chat-provider
    arm (GLM) runs without the exclusive chat-server lock the Claude arms take: its endpoint serves many
    cells at once, so a lock would only serialize them against each other."""
    path = out / "results.jsonl"
    have = read_rows(path)
    added = []
    canary = opts.get("canary")   # benchnotrun.Canary: an arm whose first cell was not run launches nothing more
    opts = {k: v for k, v in opts.items() if k != "canary"}
    reps = range(int(opts.get("reps", 1) or 1) if task.get("repeat") else 1)
    for name, arm in arms.items():
        for rep in reps:
            if (key := (task["task"], rep, name)) in have and not retry(have[key]):
                continue
            if canary and canary.before(name) == "stop":
                continue
            cell = {"task": task["task"], "rep": rep, "arm": name}
            row = None
            try:
                row = benchrun.run_cell(bench_dir, out, {**task, "task": task["task"]}, cell, arm,
                                        {**opts, "repo": Path(task["source_root"]), "state": Path(task["state"])}, cfg)
            finally:
                if canary:
                    canary.after(name, row)
            append_row(path, row, lock)
            added.append(row)
    return added


def retry(row: dict) -> bool:
    """Whether a cell's row means it runs again on a resume: the endpoint failed it, or no model ran in it."""
    return bool(row.get("endpoint_fault")) or row.get("outcome") == benchnotrun.OUTCOME


def _run_task(out: Path, bench_dir: Path, task: dict, arms: dict, opts: dict, cfg: dict, ledger: Ledger,
              lock: threading.Lock, gate: threading.Semaphore) -> list[dict]:
    """run_task under this provider's concurrency gate (a semaphore the caller owns). A test that monkey-
    patches `run_task` keeps working: the patched function is picked up by name at call time."""
    run = globals()["run_task"]
    with gate:
        return run(out, bench_dir, task, arms, opts, cfg, ledger, lock)


def run_cells(day: str, tasks: dict[str, dict], arms: dict, out: Path, bench_dir: Path, opts: dict, cfg: dict,
              cap: float, parallel=None, order: list[str] | None = None, progress=None) -> dict:
    """Run the tasks' cells. `parallel` is the number of tasks at once, a number or {provider: n} (bench.cells.*
    config; default: one per provider as before). A chat-provider cell never takes the exclusive chat-server
    lock. Returns {"ran": [task ids], "skipped": [{task, why}]}."""
    say = progress or (lambda s: None)
    out.mkdir(parents=True, exist_ok=True)
    rows = read_rows(out / "results.jsonl")
    spent = sum(float(r.get("cost_usd") or 0) for r in rows.values() if is_claude(arms.get(r["arm"], {})))
    spent += review_spent(out)
    # cap 0: no daily cap; the runaway guard (bench.daily.guard_usd, default GUARD_USD) is the stop instead,
    # and it too is off for an ordered run (an explicit task order) — nothing then stops the run for spend.
    ledger = Ledger(cap or guard_usd(cfg, bool(order)), spent, opts.get("review_mean"))
    n_claude = sum(is_claude(a) for a in arms.values())
    worst, review = n_claude * opts.get("budget_usd", CELL_USD), opts.get("review_usd", REVIEW_USD)
    limits = cells_limits(cfg) if parallel is None else parallel   # {provider: n} or a plain number
    limit = limits if isinstance(limits, dict) else {"claude": max(1, int(limits or PARALLEL)),
                                                     "chat": max(1, int(limits or PARALLEL))}
    lock = threading.Lock()
    at_once = max(1, int(parallel)) if isinstance(parallel, int) else sum(limit.values())
    ran, skipped, admitted = [], [], []
    todo = [t for t in (order or list(tasks)) if t in tasks]
    def full(t: str) -> bool:
        reps = range(int(opts.get("reps", 1) or 1)) if tasks[t].get("repeat") else range(1)
        return all((t, rep, a) in rows and not retry(rows[(t, rep, a)]) for a in arms for rep in reps)
    ran += [t for t in todo if full(t)]
    todo = [t for t in todo if not full(t)]
    ledger.finished += [sum(float(rows[(t, rep, a)].get("cost_usd") or 0) for a in arms if is_claude(arms[a])
                             for rep in range(int(opts.get("reps", 1) or 1) if tasks[t].get("repeat") else 1))
                        for t in ran]   # a resumed run's finished tasks count toward the measured mean
    if ran and not (out / "review.json").exists():
        ledger.reviews += ledger.reservation(worst, review)[1] * len(ran)   # a resumed run's finished cells are not reviewed yet: their reviews still spend
    pending: dict = {}
    # each arm's first cell of this run goes alone (benchnotrun.Canary); an arm with a row from an earlier run is cleared
    canary = benchnotrun.Canary({r["arm"] for r in rows.values() if r["arm"] in arms and not retry(r)})
    opts = {**opts, "canary": canary}
    gates: dict[str, threading.Semaphore] = {p: threading.Semaphore(max(1, n)) for p, n in limit.items()}
    def gate(arms_of_task: dict) -> threading.Semaphore:
        """A task's cells are gated by the provider that runs them: all-chat under the chat gate (GLM cells
        never wait behind Claude ones), anything else (a Claude arm present) under the claude gate."""
        only_chat = arms_of_task and all(a.get("provider") == "chat" for a in arms_of_task.values())
        return gates.get("chat" if only_chat else "claude", gates["claude"])
    with concurrent.futures.ThreadPoolExecutor(max(1, sum(limit.values()))) as pool:
        def finish(f):
            tid, w = pending.pop(f)
            try:
                got = f.result()
            except Exception as e:   # one task's fault never stops the others
                say(f"{tid}: {type(e).__name__}: {e}")
                got = []
            ledger.done(w, sum(float(r.get("cost_usd") or 0) for r in got if is_claude(arms[r["arm"]])))
            ran.append(tid)
            say(f"{tid}: done; Claude spend ${ledger.total():.2f}"
                + (f" of ${ledger.cap:.2f}" if ledger.cap else " (no cap)"))
        for tid in todo:
            while len(pending) >= at_once:
                done, _ = concurrent.futures.wait(list(pending), return_when=concurrent.futures.FIRST_COMPLETED)
                for f in done:
                    finish(f)
            while True:
                hold, rev, rule = ledger.reservation(worst, review)
                if ledger.admit(hold, rev):
                    ok = True
                    break
                if not pending:
                    ok = False
                    stop = "the runaway guard" if cap <= 0 else f"the ${cap:.0f} Claude cap"
                    skipped.append({"task": tid, "why": f"{stop} leaves no room for another task "
                                                        f"(it would reserve ${hold + rev:.2f}; {rule})"})
                    break
                done, _ = concurrent.futures.wait(list(pending), return_when=concurrent.futures.FIRST_COMPLETED)
                for f in done:
                    finish(f)
            if not ok:
                continue
            admitted.append({"task": tid, "reserved_usd": round(hold + rev, 4), "rule": rule})
            say(f"{tid}: admitted, reserves ${hold + rev:.2f} ({rule})")
            f = pool.submit(_run_task, out, bench_dir, tasks[tid], arms, opts, cfg, ledger, lock, gate(arms))
            pending[f] = (tid, hold)
        for f in list(pending):
            finish(f)
    for why in canary.stopped.values():
        say(f"ERROR: {why}")
    return {"ran": ran, "skipped": skipped, "admitted": admitted, "claude_usd": ledger.total(), "reserved_reviews": ledger.reviews,
            "stopped_arms": dict(canary.stopped)}


def prior_review_mean(root: Path, before: str | None = None) -> float | None:
    """Review spend per reviewed task in the most recent earlier day's record under <root>/days, or None."""
    for p in sorted((root / "days").glob("*.json"), reverse=True) if (root / "days").is_dir() else []:
        if before and p.stem >= before:
            continue
        try:
            rec = json.loads(p.read_text())
            tasks = {c["task"] for c in rec.get("cells") or []}
            usd = float((rec.get("spend") or {}).get("review_usd") or 0)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if tasks and usd > 0:
            return round(usd / len(tasks), 4)
    return None


def review_spent(out: Path) -> float:
    try:
        return float(json.loads((out / "review.json").read_text()).get("cost_usd") or 0)
    except (OSError, ValueError):
        return 0.0


# -- grade

def landed(score: dict, row: dict) -> bool:
    """Landed: requirements and correctness both at least LANDED_MIN of 4; a run that hit the time limit
    (or stalled) is a fail whatever its diff shows, and a cell the cheat audit flagged (a hard flag) never
    lands, whatever the graders gave its diff."""
    if row.get("hang_guard") or row.get("outcome") in ("timeout", "stalled", "error", "runner_timeout", "flagged",
                                                         benchnotrun.OUTCOME):
        return False
    s = score.get("scores") or {}
    return int(s.get("criteria", 0)) >= LANDED_MIN and int(s.get("correctness", 0)) >= LANDED_MIN


def second_reviewer(cfg: dict | None) -> dict | None:
    """The second blind grader (`bench.second_reviewer`: model, effort (default high, never above it), provider (default chat)),
    another model family than the first; None when unset. It grades every bench table's batch (the daily bench, the
    replay) again with the same masked items and rubric."""
    got = ((cfg or {}).get("bench") or {}).get("second_reviewer")
    if not isinstance(got, dict) or not isinstance(got.get("model"), str) or not got["model"].strip():
        return None
    return {"model": got["model"].strip(), "effort": blindreview.cap_effort(got.get("effort")),
            "provider": str(got.get("provider") or "chat")}


def second_grade(out: Path, cfg: dict, ask2=None, rubric=benchblind.RUBRIC, folder: str = "blind") -> dict | None:
    """Regrade the revealed batch in <out>/blind with the second reviewer into <out>/blind2 (resumable); returns
    {"reviewer", "scored": {item: (task, arm, score)}, "unscored", "compare", "review": spend} or None when there
    is none (no reviewer configured and no `ask2`, or no revealed batch). `ask2`: a reviewer callable for tests."""
    rv = second_reviewer(cfg)
    blind, blind2 = out / folder, out / f"{folder}2"
    if (rv is None and ask2 is None) or not (blind / "batch.json").exists():
        return None
    if json.loads((blind / "batch.json").read_text())["phase"] != "revealed":
        return None
    spend: dict = {}
    if not blind2.exists():
        benchblind.regrade(blind, blind2)
    if ask2 is None:
        ask2 = benchblind.provider_ask(rv["provider"], rv["model"], rv["effort"], REVIEW_USD, cfg, spend,
                                       schema=benchblind.schema_for(rubric))
    b = json.loads((blind2 / "batch.json").read_text())
    unscored = []
    for it in b["items"]:
        if it["id"] in b["scores"]:
            continue
        try:
            benchblind.score(blind2, it["id"], ask2(benchblind.prompt(it, b.get("rubric") or benchblind.RUBRIC)))
        except (benchblind.Refused, OSError, ValueError, subprocess.SubprocessError) as e:
            unscored.append({"item": it["id"], "why": str(e)[:200]})
    b = json.loads((blind2 / "batch.json").read_text())
    if len(b["scores"]) != len(b["items"]):
        return {"reviewer": rv, "scored": {}, "unscored": unscored, "compare": None, "review": spend}
    if b["phase"] == "scoring":
        b = benchblind.reveal(blind2)
    labels = json.loads((blind2 / "seal.json").read_text())["labels"]
    by_item = {it["id"]: it["task"] for it in b["items"]}
    prior = 0.0
    try:
        prior = float(json.loads((out / ("review2.json" if folder == "blind" else f"review2-{folder}.json")).read_text()).get("cost_usd") or 0)
    except (OSError, ValueError):
        pass
    durable_write(out / ("review2.json" if folder == "blind" else f"review2-{folder}.json"), json.dumps({"cost_usd": round(prior + float(spend.get("cost_usd", 0)), 4),
                                                   "calls": spend.get("calls", 0)}))
    return {"reviewer": rv, "scored": {i: (by_item[i], labels[i], s) for i, s in b["scores"].items()},
            "unscored": unscored, "compare": benchblind.compare(blind, blind2), "review": spend}


def _grade_batch(blfolder: Path, ask) -> dict:
    """Score and reveal one sealed copy of the batch in `blfolder` (a regrade folder)."""
    b = json.loads((blfolder / "batch.json").read_text())
    unscored = []
    for it in b["items"]:
        if it["id"] in b["scores"]:
            continue
        try:
            benchblind.score(blfolder, it["id"], ask(benchblind.prompt(it, b.get("rubric") or benchblind.RUBRIC)))
        except (benchblind.Refused, OSError, ValueError, subprocess.SubprocessError) as e:
            unscored.append({"item": it["id"], "why": str(e)[:200]})
    b = json.loads((blfolder / "batch.json").read_text())
    if len(b["scores"]) == len(b["items"]) and b["phase"] == "scoring":
        b = benchblind.reveal(blfolder)
    return b


def blind_review(out: Path, tasks: dict, arms: dict, cfg: dict, ask=None, seed: int | None = None,
                 build=None, rubric=benchblind.RUBRIC, ask2=None, folder: str = "blind", grades: int = 1,
                 reps: list[int] | None = None) -> dict:
    """Seal the finished cells, have the Opus reviewer score each blind, and return {item: (task, arm, score)}
    once revealed; an item the reviewer could not score stays out of the table. `ask`: a reviewer
    callable for tests. `build(out, tasks, arms)` and `rubric`: another kind of cell and its rubric (the
    offline replay's answers); the default builds the diff cells. With a second reviewer (`bench.second_reviewer`,
    or `ask2`) the revealed batch is graded again, and the result's "second" holds that grading."""
    blind = out / folder
    spend: dict = {}
    if not blind.exists():
        built = build(out, tasks, arms) if build else benchrun.blind_cells(out, tasks, arms,
                                                                             [{"rep": r} for r in (reps or [0])])
        if not built["cells"]:
            return {"scored": {}, "unscored": [], "review": spend}
        durable_write(out / ("cells.json" if folder == "blind" else f"cells-{folder}.json"), json.dumps(built, indent=1) + "\n")
        benchblind.prep(built["cells"], blind, seed, rubric)
    if ask is None:
        prov, model, eff = REVIEWER
        ask = benchblind.provider_ask(prov, model, eff, REVIEW_USD, cfg, spend, schema=benchblind.schema_for(rubric))
    b = json.loads((blind / "batch.json").read_text())
    unscored = []
    for it in b["items"]:
        if it["id"] in b["scores"]:
            continue
        try:
            benchblind.score(blind, it["id"], ask(benchblind.prompt(it, b.get("rubric") or benchblind.RUBRIC)))
        except (benchblind.Refused, OSError, ValueError, subprocess.SubprocessError) as e:
            unscored.append({"item": it["id"], "why": str(e)[:200]})
    reg = {}   # extra gradings: {item: (task, arm, score)} per copy, for grade-to-grade noise (v2)
    for i in range(1, grades):   # copies after the first: independent gradings of the same diffs
        sub = out / f"{folder}-grade{i + 1}"
        if not sub.exists():
            benchblind.regrade(blind, sub)
        bb = _grade_batch(sub, ask)
        labels = json.loads((sub / "seal.json").read_text())["labels"]
        by_item = {it["id"]: it["task"] for it in bb["items"]}
        reg[i + 1] = {i2: (by_item[i2], labels[i2], sc) for i2, sc in bb["scores"].items()}
    prior = review_spent(out) if folder == "blind" else 0.0
    durable_write(out / ("review.json" if folder == "blind" else f"review-{folder}.json"), json.dumps({"cost_usd": round(prior + float(spend.get("cost_usd", 0)), 4),
                                                  "calls": spend.get("calls", 0)}))
    b = json.loads((blind / "batch.json").read_text())
    try:
        if len(b["scores"]) == len(b["items"]):
            b = benchblind.reveal(blind) if b["phase"] == "scoring" else b
    except benchblind.Refused:
        pass
    seal = json.loads((blind / "seal.json").read_text())["labels"]   # the review is over: the labels may be read
    by_item = {it["id"]: it["task"] for it in b["items"]}
    got = {"scored": {i: (by_item[i], seal[i], s) for i, s in b["scores"].items()}, "unscored": unscored,
           "review": spend, **({"regrades": reg} if reg else {})}
    if b["phase"] == "revealed" and (sec := second_grade(out, cfg, ask2, rubric, folder)):
        got["second"] = sec
    return got


def server_cells(rows: dict, tasks: dict) -> list[dict]:
    """The cells of `rows` (read_rows) the server cut and no rerun has replaced: named in the day's record."""
    return [{"project": (tasks.get(r["task"]) or {}).get("project", ""), "task": r["task"], "arm": r["arm"],
             "model": f"{r.get('provider')}:{r.get('model')}:{r.get('effort')}", "status": serverhealth.NOT_SCORED,
             "why": r.get("server_reason") or [], "was": r.get("was")}
            for (t, rep, a), r in sorted(rows.items(), key=lambda kv: str(kv[0])) if r.get("outcome") == serverhealth.OUTCOME]


def not_run_cells(rows: dict, tasks: dict, arms: dict) -> list[dict]:
    """The cells of `rows` no model ran in (never launched, zero model calls, the server broke it): named in the
    day's record with the reason; a model that ran and left no diff is not among them, it failed."""
    return [benchnotrun.cell_record(r, (tasks.get(r["task"]) or {}).get("project", ""),
                                    f"{r.get('provider')}:{r.get('model')}:{r.get('effort')}"
                                    + (f":{arms[r['arm']]['harness']}" if (arms.get(r["arm"]) or {}).get("harness") else ""))
            for (t, rep, a), r in sorted(rows.items(), key=lambda kv: str(kv[0])) if r.get("outcome") == benchnotrun.OUTCOME]


def day_record(day: str, tasks: dict, arms: dict, out: Path, graded: dict, skipped: list, dropped: list) -> dict:
    """The day's rows, one per task and arm that was reviewed: project, scores, landed, cost, minutes."""
    rows = read_rows(out / "results.jsonl")
    cells = []
    for item, (tid, arm, score) in sorted(graded["scored"].items()):
        r = next((rows[k] for k in rows if k[0] == tid and k[2] == arm), rows.get((tid, 0, arm)) or {})
        t = tasks.get(tid) or {}
        cells.append({"project": t.get("project", ""), "task": tid, "source_task": t.get("source_task"),
                      "rep": int(r.get("rep", 0)), "arm": arm,
                      "model": f"{arms[arm].get('provider')}:{arms[arm].get('model')}:{arms[arm].get('effort')}"
                               + (f":{arms[arm]['harness']}" if arms[arm].get("harness") else ""),
                      "claude": is_claude(arms[arm]),
                      "criteria": int(score["scores"]["criteria"]), "correctness": int(score["scores"]["correctness"]),
                      "tests": int(score["scores"].get("tests", 0)), "scope": int(score["scores"].get("scope", 0)),
                      "landed": landed(score, r), "cost_usd": float(r.get("cost_usd") or 0),
                      "minutes": round(float(r.get("wall_s") or 0) / 60, 2),
                      "stopped": bool(r.get("hang_guard")) or r.get("outcome") in ("timeout", "stalled"),
                      "outcome": r.get("outcome", ""),
                      **({"flags": benchrun.hard_flags(r["flagged"])}
                         if r.get("outcome") == "flagged" and r.get("flagged") else {})})
    regrades = graded.get("regrades") or {}
    reps_graded = {tid: [] for tid in tasks}
    for gi, items in regrades.items():
        for _, (tid, arm, sc) in items.items():
            reps_graded.setdefault(tid, []).append((arm, sc))
    sec = graded.get("second")
    if sec:
        two = {(t, a): sc for t, a, sc in sec["scored"].values()}
        for c in cells:
            sc = (two.get((c["task"], c["arm"])) or {}).get("scores")
            if sc:
                c["second_criteria"], c["second_correctness"] = int(sc["criteria"]), int(sc["correctness"])
                c["second_landed"] = landed({"scores": sc}, rows.get((c["task"], 0, c["arm"])) or {})
    claude_cells = sum(c["cost_usd"] for c in cells if c["claude"])
    # Cells the server cut: not scored and never a fail, queued for a rerun (serverhealth.py); named, not counted.
    server = server_cells(rows, tasks)
    # Cells no model ran in (benchnotrun.py): their own count, outside every pass-rate denominator, never reviewed.
    not_run = not_run_cells(rows, tasks, arms)
    return {"day": day, "cells": cells, **({"server": server} if server else {}), **({"not_run": not_run} if not_run else {}),
            "skipped": skipped, "dropped": dropped,
            "unscored": graded.get("unscored", []),
            **({"second_grader": {"reviewer": sec["reviewer"], "compare": sec["compare"],
                                  "unscored": sec["unscored"]}} if sec else {}),
            "spend": {"claude_usd": round(claude_cells + review_spent(out), 4),
                      "claude_arms_usd": round(claude_cells, 4), "review_usd": round(review_spent(out), 4),
                      "glm_usd": round(sum(c["cost_usd"] for c in cells if not c["claude"]), 4)}}


# -- review-fix round (benchfix.py)

def fix_round(out: Path, bench_dir: Path, tasks: dict, arms: dict, graded: dict, opts: dict, cfg: dict, reps: int = 1,
              ask=None, ask2=None, guard: float | None = None, say=None) -> dict | None:
    """The real pipeline's review-fix loop, once: every arm cell the first blind review did not land gets one fix
    run with the reviewer's findings, and the fixed diffs are graded again in a blind batch of their own
    (`blind-fix`). Returns {"cells", "skipped", "unscored", "spend"} for the day's record, or None when
    `bench.fix.enabled` is off. Resumable: a fix with a row is not run again, a finished batch is kept."""
    if not benchfix.enabled(cfg):
        return None
    say = say or (lambda s: None)
    rows = read_rows(out / "results.jsonl")
    first = benchfix.first_cells(graded, rows, tasks, arms, reps, benchpack.ORIGINAL, landed)
    need = benchfix.needing_fix(first)
    fixrows, skipped = read_rows(out / benchfix.FILE), []
    if need:
        spent = sum(float(r.get("cost_usd") or 0) for r in rows.values() if is_claude(arms.get(r["arm"], {}))) + review_spent(out)
        got = benchfix.run_fixes(out, bench_dir, tasks, arms, need, opts, cfg, benchrun.run_cell, read_rows, append_row,
                                 cells_limits(cfg), guard, spent, is_claude, say)
        fixrows, skipped = got["rows"], got["skipped"]
    regraded = {"scored": {}, "unscored": [], "review": {}}
    if any(benchfix.fixed_ok(r) for r in fixrows.values()):
        regraded = blind_review(out, tasks, arms, cfg, ask, build=benchfix.regrade_build(fixrows, reps, benchrun.blind_cells),
                                ask2=ask2, folder=benchfix.FOLDER)
    cells = benchfix.cell_rows(first, fixrows, regraded, landed)
    review = 0.0
    try:
        review = float(json.loads((out / f"review-{benchfix.FOLDER}.json").read_text()).get("cost_usd") or 0)
    except (OSError, ValueError):
        pass
    return {"cells": cells, "skipped": skipped, "unscored": regraded.get("unscored", []),
            "spend": {"claude_usd": round(sum(c["fix_usd"] for c in cells if c["fix_claude"]), 4),
                      "glm_usd": round(sum(c["fix_usd"] for c in cells if not c["fix_claude"]), 4),
                      "review_usd": round(review, 4)}}


def add_fix_spend(rec: dict, fix: dict) -> None:
    """The fix round's spend joins the day's: Claude fixes and the regrade review count toward the Claude total."""
    sp, f = rec.setdefault("spend", {}), fix["spend"]
    sp["fix_claude_usd"], sp["fix_glm_usd"], sp["fix_review_usd"] = f["claude_usd"], f["glm_usd"], f["review_usd"]
    sp["claude_usd"] = round(float(sp.get("claude_usd") or 0) + f["claude_usd"] + f["review_usd"], 4)


# -- cumulative table

def cumulative(root: Path | None = None) -> dict:
    """The running table from every day's record under `root`/days: per project and arm, and per arm overall."""
    root = root or home()
    cells, gaps, seconds, server, reruns, limits, fixed, idle = [], [], [], [], [], {}, [], []
    orig, gone, left = {"n": 0, "passed": 0, "per_task": {}}, [], []
    days = sorted((root / "days").glob("*.json")) if (root / "days").exists() else []
    for p in days:
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        cells += [{**c, "day": p.stem} for c in rec.get("cells") or []]
        fixed += [{**c, "day": p.stem} for c in (rec.get("fix_round") or {}).get("cells") or []]
        limits[p.stem] = rec.get("limit_s")
        if o := rec.get("original"):   # the original landed change, graded blind with the arms (benchpack)
            orig["n"] += o["n"]
            orig["passed"] += o["passed"]
            orig["per_task"].update({f"{p.stem}/{t}": v for t, v in o["per_task"].items()})
        gone += [{"day": p.stem, **x} for x in rec.get("dropped_original") or []]
        left += [{"day": p.stem, **x} for x in rec.get("not_code_bench") or []]
        server += [{"day": p.stem, **x} for x in rec.get("server") or []]
        idle += [{"day": p.stem, **x} for x in rec.get("not_run") or []]
        reruns += [{"day": p.stem, **x} for x in rec.get("reruns") or []]
        if (cmp := (rec.get("second_grader") or {}).get("compare")):
            seconds.append({"day": p.stem, "ranking_changed": cmp["ranking_changed"], "rank_a": cmp["rank_a"],
                            "rank_b": cmp["rank_b"]})
        if rec.get("faults") or rec.get("skipped"):
            gaps.append({"day": p.stem, "faults": rec.get("faults") or [], "skipped": len(rec.get("skipped") or [])})

    def agg(rows: list[dict]) -> dict:
        n = len(rows)
        # a flagged cell is never landed, also in a day file written before landed() knew it
        return {"n": n, "landed": sum(1 for r in rows if r["landed"] and r.get("outcome") != "flagged"),
                **({"flagged": f} if (f := sum(1 for r in rows if r.get("outcome") == "flagged")) else {}),
                "criteria": round(sum(r["criteria"] for r in rows) / n, 2),
                "correctness": round(sum(r["correctness"] for r in rows) / n, 2),
                "usd": round(sum(r["cost_usd"] for r in rows), 2),
                "usd_per_task": round(sum(r["cost_usd"] for r in rows) / n, 2),
                "min_per_task": round(sum(r["minutes"] for r in rows) / n, 1),
                "stops": sum(1 for r in rows if r["stopped"]),
                **({"second_n": len(two),
                    "second_landed": sum(1 for r in two if r["second_landed"] and r.get("outcome") != "flagged"),
                    "second_criteria": round(sum(r["second_criteria"] for r in two) / len(two), 2),
                    "second_correctness": round(sum(r["second_correctness"] for r in two) / len(two), 2)}
                   if (two := [r for r in rows if "second_landed" in r]) else {})} if n else {}
    by, per = {}, {}
    for c in cells:
        by.setdefault((c["project"], c["model"]), []).append(c)
        per.setdefault(c["model"], []).append(c)
    # a not-run cell (this check's and a server cut) is its own count per model, outside n and every rate
    gone_by, gone_per = {}, {}
    for x in idle + server:
        gone_by[(x["project"], x["model"])] = gone_by.get((x["project"], x["model"]), 0) + 1
        gone_per[x["model"]] = gone_per.get(x["model"], 0) + 1

    def with_idle(a: dict, k: int) -> dict:
        return {**(a or {"n": 0, "landed": 0, "criteria": 0, "correctness": 0, "usd": 0, "usd_per_task": 0,
                         "min_per_task": 0, "stops": 0}), **({"not_run": k} if k else {})}
    projects = sorted({c["project"] for c in cells} | {pr for pr, _ in gone_by})
    return {"days": [p.stem for p in days],
            "projects": {proj: {m: with_idle(agg(by[(pr, m)]) if (pr, m) in by else None, gone_by.get((pr, m), 0))
                                for (pr, m) in sorted(set(by) | set(gone_by)) if pr == proj}
                         for proj in projects},
            "models": {m: with_idle(agg(per[m]) if m in per else None, gone_per.get(m, 0))
                       for m in sorted(set(per) | set(gone_per))}, "gaps": gaps,
            **({"server": server} if server else {}),
            **({"not_run": idle} if idle else {}),
            **({"reruns": reruns} if reruns else {}),
            **({"original": orig} if orig["n"] else {}),
            **({"fix_round": benchfix.summary(fixed)} if fixed else {}),
            **({"dropped_original": gone} if gone else {}),
            **({"not_code_bench": left} if left else {}),
            **({"flagged": flagged} if (flagged := [
                {"day": c["day"], "project": c["project"], "task": c["task"], "arm": c["arm"], "flags": c.get("flags") or []}
                for c in cells if c.get("outcome") == "flagged"]) else {}),
            "parity": [parity_row(m, rs, limits) for m, rs in sorted(per.items())],
            **({"second_days": seconds} if seconds else {})}


PARITY_FIELDS = ("task_set", "tasks", "limit_s", "limit_counts", "rerun_rule")
RERUN_RULE = "a cell the server cut is not scored and rerun one at a time once the server is healthy"


def parity_row(key: str, cells: list[dict], limits: dict) -> dict:
    """One model's conditions for the parity check (benchparity): its arm (provider:model:effort[:harness]),
    the cells it ran (day/task) as the task set and the limit of each day it ran (one value, or each day's)."""
    from . import benchparity
    prov, model, effort, *h = key.split(":") + [""] * 3
    lim = sorted({str(limits.get(c.get("day"))) for c in cells if limits.get(c.get("day")) is not None})
    cli = prov or "claude"   # an arm with no harness runs its provider's own CLI; the chat one calls the run's bridge
    kind = ("chat endpoint through the run's bridge" if h[0] else
            "the chat provider's CLI through the run's bridge" if cli == "chat" else "provider CLI")
    return {"arm": key, "model": model, "provider": prov, "effort": effort, "harness": h[0] or f"{cli} (the provider's CLI)",
            "agent": h[0] or cli, "kind": kind,
            "task_set": benchparity.task_set([f"{c.get('day')}/{c['task']}" for c in cells]), "tasks": len(cells),
            "limit_s": "/".join(lim) if lim else None, "limit_counts": "the cell's wall time (a stop is a fail)",
            "rerun_rule": RERUN_RULE}


def _row(label: str, a: dict, second: bool = False) -> str:
    tail = (f" {a['second_landed']}/{a['second_n']} ({a['second_criteria']}/{a['second_correctness']}) |"
            if a.get("second_n") else " |") if second else ""
    return (f"| {label} | {a['n']} | {a['landed']}/{a['n']} | {a['criteria']} | {a['correctness']} | "
            f"${a['usd']} | ${a['usd_per_task']} | {a['min_per_task']} | {a['stops']} | {a.get('not_run', 0)} |{tail}")


HEAD = ("| {} | N | landed | requirements (0-4) | correctness (0-4) | $ total | $ per task | minutes per task | 60-min stops | not run |{}\n"
        "|---|---|---|---|---|---|---|---|---|---|{}")


def table_md(cum: dict) -> str:
    """The cumulative table as markdown: per model overall, then per project. A second grader's cells add a
    column: landed of those it graded (mean requirements/correctness)."""
    two = any(a.get("second_n") for a in cum["models"].values())
    head = HEAD.format("model", " second grader: landed (req/corr) |" if two else "", "---|" if two else "")
    lines = [f"# Daily model bench: cumulative ({len(cum['days'])} day(s): {', '.join(cum['days']) or 'none'})", "",
             "v1: cannot separate the models.", "",
             "Landed = requirements and correctness each at least 3 of 4 by the blind Opus review; a 60-minute stop is "
             "a fail, and so is a model that worked and left no diff. Not run = no model ran in the cell (never launched, "
             "zero model calls, or the server broke it): its own count, outside N and every rate. $ per task is each arm's own spend (an open model's is GLM spend, not Claude spend).", "",
             "## Per model", "", head]
    lines += [_row(m, a, two) for m, a in cum["models"].items()]
    for proj, ms in cum["projects"].items():
        lines += ["", f"## {proj}", "", head]
        lines += [_row(m, a, two) for m, a in ms.items()]
    if cum.get("original"):
        o = cum["original"]
        lines += ["", "## Original change", "",
                  f"The change that really landed, graded blind in the same batch as the arms: {o['passed']}/{o['n']} "
                  "passed. A task whose original does not pass is out of the table (listed below); the arms are "
                  "compared on the rest."]
    if cum.get("fix_round"):
        lines += [""] + benchfix.section(cum["fix_round"])
    if cum.get("dropped_original"):
        lines += ["", "## Dropped: the original change did not pass", ""]
        lines += [f"- {x['day']}: {x['project']} {x['task']}: {x['why']}" for x in cum["dropped_original"]]
    if cum.get("not_code_bench"):
        lines += ["", "## Not in the code bench", "",
                  "Tasks whose original worker's context cannot be rebuilt (live-only: it needed a live system; "
                  "invalid: its records or its criteria cannot support a fair comparison):"]
        lines += [f"- {x['day']}: {x['project']} {x['task']}: {x['class']}: {x['why']}" for x in cum["not_code_bench"]]
    if cum.get("server"):
        lines += ["", f"## {serverhealth.NOT_SCORED[0].upper() + serverhealth.NOT_SCORED[1:]}", "",
                  "Cells the model server cut (the limit, the wall ceiling or a capacity refusal while it ran slower than "
                  "the model's normal speed). They are never counted as fails; each is rerun, one at a time, once the "
                  "server is healthy:"]
        lines += [f"- {x['day']}: {x['project']} {x['task']} {x['arm']} ({x['model']}): {'; '.join(x.get('why') or [])}"
                  for x in cum["server"]]
    if cum.get("not_run"):
        lines += ["", "## Not run", "", "Cells no model ran in. They are never counted done or failed and are outside "
                  "every pass rate; the count per arm is in the tables above:"]
        lines += [f"- {x['day']}: {x['project']} {x['task']} {x['arm']} ({x['model']}): {x['why']}" for x in cum["not_run"]]
    if cum.get("flagged"):
        lines += ["", "## Flagged by the cheat audit", "", "Cells whose own tool calls ran a history or forge command or "
                  "named a hidden path. They are shown, graded, and never counted as landed:"]
        lines += [f"- {x['day']}: {x['task']} {x['arm']}: {'; '.join(x['flags']) or 'flagged'}" for x in cum["flagged"]]
    if cum.get("reruns"):
        lines += ["", "## Server reruns", "", "Stops rechecked against the server rule, each rerun at normal server speed; "
                  "the cause says whether the server or the model stopped the first run:"]
        lines += [f"- {x['day']}: {x['task']} {x['arm']}: {x.get('rerun_outcome') or 'not rerun'} on rerun (first run "
                  f"{(x.get('was') or {}).get('outcome') or 'stopped'}; {CAUSE_TEXT.get(x['cause'], x['cause'])})"
                  for x in cum["reruns"]]
    if cum.get("parity"):
        from . import benchparity
        # the harness is part of what an arm compares (the harness A/B), so only the conditions must match
        lines += [""] + benchparity.section(cum["parity"], fields=PARITY_FIELDS)
    if cum.get("second_days"):
        lines += ["", "## Second grader", "", "The same masked batch graded again by a second model; the ranking is by mean "
                  "total, best first:"]
        lines += [f"- {d['day']}: ranking {'changed' if d['ranking_changed'] else 'unchanged'} "
                  f"({' > '.join(d['rank_a'])}; second: {' > '.join(d['rank_b'])})" for d in cum["second_days"]]
    if cum.get("gaps"):
        lines += ["", "## Not covered", "", "A project whose landings the bench could not read, and tasks the cap left out:"]
        for g in cum["gaps"]:
            lines += [f"- {g['day']}: {f['project']}: {f['why']}" for f in g["faults"]]
            lines += [f"- {g['day']}: {g['skipped']} task(s) skipped by the Claude cap"] if g["skipped"] else []
    return "\n".join(lines) + "\n"


def write_table(root: Path | None = None) -> dict:
    root = root or home()
    cum = cumulative(root)
    durable_write(root / "cumulative.json", json.dumps(cum, indent=1) + "\n")
    durable_write(root / "table.md", table_md(cum))
    return cum


# -- the day

def parse_arms(extra: list[str] | None, glm_harness: str | None = None) -> dict:
    """The arms by name. The open-model arm runs on `glm_harness` (None: GLM_HARNESS; "" or "claude": the Claude
    Code bridge; opencode, mini-swe-agent); `extra` (--arm texts) replaces the whole list."""
    harness = GLM_HARNESS if glm_harness is None else (glm_harness or "claude")
    texts = list(extra) if extra else [*ARMS, f"{GLM_ARM}:{harness}"]
    return dict(benchrun.parse_arm(x) for x in texts)


def pack_keep(rows: dict) -> dict:
    """{task: sha256} of the context pack the cells already run used (results.jsonl rows, by (task, rep, arm)):
    a resumed result set keeps its pack, so the arms of one repeat set never mix packs."""
    return {str(k[0]): r["pack_sha256"] for k, r in sorted(rows.items(), key=lambda kv: str(kv[0])) if r.get("pack_sha256")}


def build_tasks(day: str, picked: dict, root: Path, bench_dir: Path, skip: str | None = None,
                left: list | None = None, keep: dict | None = None) -> dict[str, dict]:
    """The day's bench tasks (build) of every picked project, from picked.json and the project mirrors. Each gets
    its context pack (benchpack); a task that is live-only or invalid is left out and, with `left`, listed there."""
    projs = {p["name"]: p for p in projects(skip)}
    tasks: dict[str, dict] = {}
    for name in sorted({k["project"] for k in picked["order"]}):
        repo = root / "mirrors" / slug(name)
        tasks.update(build(day, projs[name], [k for k in picked["order"] if k["project"] == name], repo, bench_dir))
    kept, out = benchpack.screen(tasks, {n: open_ro(projs[n]["db"]) for n in {t["project"] for t in tasks.values()}},
                                 bench_dir, keep)
    if left is not None:
        left += out
    return kept


def run_day(day: str, start: float, end: float, arms: dict, cfg: dict, cap: float = CAP_USD, parallel=None,
            seed: int | None = None, skip: str | None = None, root: Path | None = None, only: list[str] | None = None,
            opts: dict | None = None, ask=None, progress=None, repos: dict | None = None, ask2=None) -> dict:
    """The whole day: pick across projects, build, run under the cap, review, write the day's record and the
    cumulative table. Resumable: picked.json, results.jsonl and the blind folder under <root>/<day> are reused."""
    say = progress or (lambda s: print(s, file=sys.stderr, flush=True))
    root = root or home()
    out = root / "runs" / day
    out.mkdir(parents=True, exist_ok=True)
    seed = seed if seed is not None else int(day.replace("-", ""))
    pfile = out / "picked.json"
    bench_dir = out / "bench"
    if pfile.exists():
        picked = json.loads(pfile.read_text())
    else:
        by_project, dropped, faults = {}, [], []
        for proj in projects(skip):
            if only and proj["name"] not in only:
                continue
            try:
                db = open_ro(proj["db"])
                if not landed_in_window(db, start, end):
                    say(f"bench daily: {proj['name']}: nothing landed that day in the records the bench reads")
                    by_project[proj["name"]] = []
                    continue
                src = repo_of(proj, repos)
                if src is None:
                    raise RuntimeError("no git repository at the project root (name one in bench.daily.repos)")
                repo = mirror(proj, root / "mirrors", src)
                kept, drops = pick(db, repo, start, end)
            except Exception as e:   # one project's trouble never stops the others
                faults.append({"project": proj["name"], "why": f"{type(e).__name__}: {e}"[:300]})
                say(f"bench daily: {proj['name']}: {e}")
                continue
            for d in drops:
                dropped.append({"project": proj["name"], **d})
            for k in kept:
                k["project"] = proj["name"]
            by_project[proj["name"]] = kept
            say(f"bench daily: {proj['name']}: {len(kept)} kept, {len(drops)} dropped")
        picked = {"day": day, "order": sample(by_project, seed), "dropped": dropped, "faults": faults}
        durable_write(pfile, json.dumps(picked, indent=1) + "\n")
    for name, arm in arms.items():
        benchrun.check_harness(name, arm)
    arms = {name: benchrun.resolve_harness(arm, opts or {}) for name, arm in arms.items()}
    left: list[dict] = []
    tasks = build_tasks(day, picked, root, bench_dir, skip, left, pack_keep(read_rows(out / "results.jsonl")))
    v2cfg = (cfg.get("bench") or {}).get("repeats") or {}
    reps = max(1, int(v2cfg.get("runs", 1)))
    grades = max(1, int(v2cfg.get("grades", 1)))
    cap_n = int(v2cfg.get("subset", 0) or 0)
    # The repeat subset is drawn first, seeded, so every arm and every grade covers the same tasks.
    repeat_ids = sorted(tasks)[:cap_n] if cap_n and len(tasks) > cap_n else sorted(tasks) if cap_n else []
    rng = random.Random(seed)
    rng.shuffle(repeat_ids)
    repeat_ids = set(repeat_ids)
    for tid in tasks:
        tasks[tid]["repeat"] = tid in repeat_ids
    order = [f"{slug(k['project'])}-{k['task']}" for k in picked["order"] if f"{slug(k['project'])}-{k['task']}" in tasks]
    opts = {"timeout_s": LIMIT_S, "stall_s": 900, "budget_usd": CELL_USD, "review_usd": REVIEW_USD,
            "test_cmd": benchrun.TEST_CMD, "test_timeout_s": 900, "dry_run": False, "scratch_home": True,
            "review_mean": prior_review_mean(root, day), "reps": reps, **(opts or {})}
    if opts.get("reps", reps) != reps:
        raise RuntimeError("an explicit --reps opt overrides the repeats.runs setting; set bench.repeats.runs")
    done = run_cells(day, tasks, arms, out, bench_dir, opts, cfg, cap, parallel, order, say)
    benchpack.add_original(out, tasks)
    graded = blind_review(out, tasks, {**arms, benchpack.ORIGINAL: benchpack.ORIGINAL_ARM}, cfg, ask, ask2=ask2,
                          grades=grades, reps=list(range(reps)))
    skipped = picked.get("skipped", []) + done["skipped"]
    rec = day_record(day, tasks, {**arms, benchpack.ORIGINAL: benchpack.ORIGINAL_ARM}, out, graded, skipped,
                     picked["dropped"])
    rec["cells"], gone, rec["original"] = benchpack.split_original(rec["cells"])
    if fix := fix_round(out, bench_dir, tasks, arms, graded, opts, cfg, reps, ask, ask2, guard_usd(cfg, bool(order)), say):
        rec["fix_round"] = {k: fix[k] for k in ("cells", "skipped", "unscored")}
        add_fix_spend(rec, fix)
    rec["dropped_original"] = gone
    rec["not_code_bench"] = left   # live-only or invalid: left out of the code bench, with why
    rec["faults"] = picked.get("faults", [])
    rec["admitted"] = done["admitted"]   # which reservation rule admitted each task
    if done.get("stopped_arms"):   # an arm whose first cell was not run launched nothing more (benchnotrun.Canary)
        rec["stopped_arms"] = done["stopped_arms"]
    rec["limit_s"] = opts["timeout_s"]   # the parity check's limit (cumulative)
    rec["arms"] = {k: {f: v.get(f, "") for f in ("provider", "model", "effort", "harness")} for k, v in arms.items()}
    durable_write(root / "days" / f"{day}.json", json.dumps(rec, indent=1) + "\n")
    write_table(root)
    return rec


RERUN_WHY = ("recorded before the server's waits were measured: whether the model server was slow is unknown; "
             "the rerun at normal speed decides")


CAUSE_TEXT = {"server": "the server stopped the first run", "model": "the model stopped it again at normal server speed",
              "server (still slow)": "the server is still slow, queued again",
              "server (not rerun yet)": "queued, not rerun yet"}


def stop_cause(new: dict) -> str:
    """Who stopped an earlier stop, from the cell that reran it: a rerun that ended inside the limit means the
    server cut the first run ("server"); one that stopped again with the server at normal speed means the model
    ("model"); one the server cut again stays "server (still slow)"."""
    if new.get("outcome") == serverhealth.OUTCOME:
        return "server (still slow)"
    if new.get("hang_guard") or new.get("outcome") in ("timeout", "stalled"):
        return "model"
    return "server"


def _blind_build(sub_tasks: dict, sub_arms: dict):
    """A blind batch of only the rerun arms: each arm's cells built alone, so a task whose other arm is still
    queued for a rerun is not left out."""
    def build(out, tasks, arms):
        merged: dict[str, dict] = {}
        for name, arm in sub_arms.items():
            for c in benchrun.blind_cells(out, sub_tasks, {name: arm}, [{"rep": 0}])["cells"]:
                m = merged.setdefault(c["task"], {"task": c["task"], "criteria": c["criteria"], "arms": {}})
                m["arms"].update(c["arms"])
        return {"cells": list(merged.values())}
    return build


def _rerun_folder(out: Path) -> str:
    """The blind folder of this rerun: the last one while it is unfinished, else a new one."""
    names = sorted((x.name for x in out.glob("blind-rerun*") if x.is_dir() and not x.name.endswith("2")),
                   key=lambda n: int(n[len("blind-rerun"):] or 1))
    if names:
        try:
            if json.loads((out / names[-1] / "batch.json").read_text())["phase"] != "revealed":
                return names[-1]
        except (OSError, ValueError, KeyError):
            return names[-1]
    return f"blind-rerun{len(names) + 1}" if names else "blind-rerun"


def rerun_day(day: str, stops: list[tuple[str, str]], root: Path | None = None, cfg: dict | None = None,
              opts: dict | None = None, arms: dict | None = None, probe=None, lock=None, sleep=time.sleep, ask=None,
              ask2=None, progress=None, skip: str | None = None) -> dict:
    """Check an earlier day's stops against the server rule, through the bench's own code. `stops` are (task, arm)
    pairs (a task is its id or its source task number). Each stop's cell is marked `server` (not scored, never a
    fail), rerun one at a time once the server is healthy (benchrun.rerun_cells), graded in a blind batch of its
    own (the day's first batch stays as it was), and merged into the day's record: the new scores replace the stop,
    `reruns` says for each whether the server or the model stopped it (stop_cause), and the cumulative table is
    written again. Returns the day's record."""
    say = progress or (lambda s: print(s, file=sys.stderr, flush=True))
    root = root or home()
    out = root / "runs" / day
    day_file = root / "days" / f"{day}.json"
    rec = json.loads(day_file.read_text())
    picked = json.loads((out / "picked.json").read_text())
    bench_dir = out / "bench"
    if arms is None:
        arms = {n: benchrun.parse_arm(f"{n}={a['provider']}:{a['model']}:{a['effort']}"
                                      + (f":{a['harness']}" if a.get("harness") else ""))[1]
                for n, a in (rec.get("arms") or {}).items()}
    arms = {name: benchrun.resolve_harness(arm, opts or {}) for name, arm in arms.items()}
    tasks = build_tasks(day, picked, root, bench_dir, skip, None, pack_keep(read_rows(out / "results.jsonl")))
    tasks = {t: v for t, v in tasks.items() if t not in {x["task"] for x in rec.get("dropped_original") or []}}

    def match(tid: str, want: str) -> bool:
        return tid == want or tid.endswith(f"-{want}") or str((tasks.get(tid) or {}).get("source_task")) == want
    cells = sorted({(tid, name) for tid in tasks for want, name in stops if match(tid, want) and name in arms})
    if not cells:
        raise ValueError(f"no cell of {day} matches {stops}")
    base = {"timeout_s": LIMIT_S, "stall_s": 900, "budget_usd": CELL_USD, "review_usd": REVIEW_USD,
            "test_cmd": benchrun.TEST_CMD, "test_timeout_s": 900, "dry_run": False, "scratch_home": True, **(opts or {})}
    marked = benchrun.recheck_stops(out, [(tid, 0, name) for tid, name in cells], RERUN_WHY)
    say(f"bench daily: {day}: {len(marked)} stop(s) marked as server cells, rerunning one at a time")
    added = benchrun.rerun_cells(bench_dir, out, tasks, arms, [], 0, base, cfg or {}, probe, sleep, lock, only=cells,
                                 task_opts=lambda t: {"repo": Path(t["source_root"]), "state": Path(t["state"])})
    now = read_rows(out / "results.jsonl")
    # only cells rerun now, or rerun earlier with no rerun entry yet (a call that ended before its review): a cell
    # whose rerun is already recorded is not graded again
    ran = {(r["task"], r["arm"]) for r in added if r.get("rerun_of")}
    known = {(x["task"], x["arm"]) for x in rec.get("reruns") or [] if x.get("rerun_outcome")}
    done = [(tid, name) for tid, name in cells if (r := now.get((tid, 0, name))) and r.get("outcome") != serverhealth.OUTCOME
            and ((tid, name) in ran or (r.get("rerun_of") and (tid, name) not in known))]
    graded = {"scored": {}, "unscored": []}
    sub_arms = {name: arms[name] for _, name in done}
    sub_tasks = {tid: tasks[tid] for tid, _ in done}
    folder = _rerun_folder(out)
    if done:
        graded = blind_review(out, sub_tasks, sub_arms, cfg or {}, ask, build=_blind_build(sub_tasks, sub_arms),
                              ask2=ask2, folder=folder)
    new = day_record(day, tasks, arms, out, graded, [], [])
    keep = {(c["task"], c["arm"]) for c in new["cells"]}
    rec["cells"] = [c for c in rec["cells"] if (c["task"], c["arm"]) not in keep] + new["cells"]
    prior = {(x["task"], x["arm"]): x for x in rec.get("reruns") or []}
    for r in added:
        if (r["task"], r["arm"]) in cells and r.get("rerun_of"):
            prior[(r["task"], r["arm"])] = {"task": r["task"], "arm": r["arm"], "was": (r["rerun_of"].get("was") or {}),
                                            "cause": stop_cause(r), "rerun_outcome": r.get("outcome"),
                                            "rerun_minutes": round(float(r.get("wall_s") or 0) / 60, 2)}
    for tid, name in cells:
        r = now.get((tid, 0, name)) or {}
        if (tid, name) not in prior and r.get("outcome") == serverhealth.OUTCOME:
            prior[(tid, name)] = {"task": tid, "arm": name, "was": r.get("was") or {}, "cause": "server (not rerun yet)"}
    rec["reruns"] = sorted(prior.values(), key=lambda x: (x["task"], x["arm"]))
    rec["server"] = server_cells(now, tasks)
    if not rec["server"]:
        rec.pop("server", None)
    sp = rec.setdefault("spend", {})
    extra = {k: float(new["spend"].get(k) or 0) for k in ("claude_arms_usd", "glm_usd")}
    review2 = float((json.loads((out / f"review2-{folder}.json").read_text()) if (out / f"review2-{folder}.json").exists()
                     else {}).get("cost_usd") or 0)
    review = float((json.loads((out / f"review-{folder}.json").read_text()) if (out / f"review-{folder}.json").exists()
                    else {}).get("cost_usd") or 0)
    for k, v in extra.items():
        sp[k] = round(float(sp.get(k) or 0) + v, 4)
    sp["review_usd"] = round(float(sp.get("review_usd") or 0) + review, 4)
    sp["claude_usd"] = round(float(sp.get("claude_usd") or 0) + extra["claude_arms_usd"] + review, 4)
    sp["review2_usd"] = round(float(sp.get("review2_usd") or 0) + review2, 4)
    durable_write(day_file, json.dumps(rec, indent=1) + "\n")
    write_table(root)
    return rec


def fix_day(day: str, root: Path | None = None, cfg: dict | None = None, opts: dict | None = None,
            arms: dict | None = None, ask=None, ask2=None, progress=None, skip: str | None = None) -> dict:
    """The review-fix round of an earlier day (benchfix.py), through the bench's own code: read the day's first
    review back from its blind folder (no new scoring), run one fix per cell that did not land, grade the fixed
    diffs in the `blind-fix` batch and merge the round into the day's record (`fix_round`, spend) and the
    cumulative table. Returns the day's record; a day with a round already keeps it unless cells are new."""
    say = progress or (lambda s: print(s, file=sys.stderr, flush=True))
    root = root or home()
    cfg = cfg or {}
    out = root / "runs" / day
    day_file = root / "days" / f"{day}.json"
    rec = json.loads(day_file.read_text())
    if not (out / "blind" / "batch.json").exists():
        raise ValueError(f"{day} has no blind review to read the findings from")
    picked = json.loads((out / "picked.json").read_text())
    bench_dir = out / "bench"
    if arms is None:
        arms = {n: benchrun.parse_arm(f"{n}={a['provider']}:{a['model']}:{a['effort']}"
                                      + (f":{a['harness']}" if a.get("harness") else ""))[1]
                for n, a in (rec.get("arms") or {}).items()}
    arms = {name: benchrun.resolve_harness(arm, opts or {}) for name, arm in arms.items()}
    tasks = build_tasks(day, picked, root, bench_dir, skip, None, pack_keep(read_rows(out / "results.jsonl")))
    tasks = {t: v for t, v in tasks.items() if t not in {x["task"] for x in rec.get("dropped_original") or []}}
    reps = max(1, int(((cfg.get("bench") or {}).get("repeats") or {}).get("runs", 1)))
    for tid in tasks:
        tasks[tid]["repeat"] = True
    graded = blind_review(out, tasks, {**arms, benchpack.ORIGINAL: benchpack.ORIGINAL_ARM}, cfg, ask, ask2=ask2,
                          reps=list(range(reps)))
    base = {"timeout_s": LIMIT_S, "stall_s": 900, "budget_usd": CELL_USD, "review_usd": REVIEW_USD,
            "test_cmd": benchrun.TEST_CMD, "test_timeout_s": 900, "dry_run": False, "scratch_home": True, **(opts or {})}
    fix = fix_round(out, bench_dir, tasks, arms, graded, base, cfg, reps, ask, ask2, None, say)
    if fix is None:
        raise ValueError("the review-fix round is off (bench.fix.enabled)")
    rec["fix_round"] = {k: fix[k] for k in ("cells", "skipped", "unscored")}
    sp = rec.setdefault("spend", {})
    sp["claude_usd"] = round(float(sp.get("claude_usd") or 0) - float(sp.get("fix_claude_usd") or 0)
                             - float(sp.get("fix_review_usd") or 0), 4)   # a second pass replaces the first's spend
    add_fix_spend(rec, fix)
    durable_write(day_file, json.dumps(rec, indent=1) + "\n")
    write_table(root)
    return rec


PICK_WAIT_S = 150   # how long `ttp bench daily` waits for the pick stage (under a command schedule's time limit)


def skip_lines(day: str, root: Path | None = None) -> list[str]:
    """One schedule observation (a JSON line, severity normal, key bench-daily-skip-<project>) per project the day's
    pick left out because its mirror or its records could not be prepared; [] while the pick has not finished."""
    pfile = (root or home()) / "runs" / day / "picked.json"
    try:
        faults = json.loads(pfile.read_text()).get("faults") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [json.dumps({"text": f"bench daily: {f['project']} was left out of {day}'s bench: {f['why']}",
                        "severity": "normal", "key": f"bench-daily-skip-{slug(f['project'])}"})
            for f in faults if isinstance(f, dict) and f.get("project")]


def wait_skips(day: str, root: Path | None = None, wait_s: float = PICK_WAIT_S, poll_s: float = 2.0) -> list[str]:
    """`skip_lines` once the pick stage has written picked.json, waiting up to `wait_s` seconds for it."""
    pfile = (root or home()) / "runs" / day / "picked.json"
    end = time.monotonic() + wait_s
    while not pfile.exists() and time.monotonic() < end:
        time.sleep(min(poll_s, max(0.0, end - time.monotonic())))
    return skip_lines(day, root)


def lock(day: str, root: Path | None = None):
    """A held lock file for the day (None when another run holds it): one run per day on this host."""
    root = root or home()
    (root / "runs" / day).mkdir(parents=True, exist_ok=True)
    f = open(root / "runs" / day / "run.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def detach(argv: list[str], day: str, project_root: Path, root: Path | None = None,
           command: str = "daily") -> tuple[bool, Path]:
    """Start `python -m ttp bench <command> --here <argv>` as its own session, log under <root>/runs/<day>/; returns
    (started, log). Not started when a run for the day already holds its lock."""
    root = root or home()
    probe = lock(day, root)
    log = root / "runs" / day / "run.log"
    if probe is None:
        return False, log
    probe.close()    # the child takes the lock itself
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent) + os.pathsep
           + os.environ.get("PYTHONPATH", "")}
    with open(log, "ab") as lf:
        # own session: the bench run outlives the caller that started it and is not stopped with it
        subprocess.Popen([sys.executable, "-m", "ttp", "bench", command, "--here", *argv], cwd=project_root, env=env,
                         stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    return True, log
