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

from . import bench, benchblind, benchrun, effort, gitmemo
from .project import durable_write, open_append

CAP_USD = 40.0       # Claude spend a day (arm runs plus reviews)
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


def mirror(proj: dict, base: Path | None = None, source: Path | None = None) -> Path:
    """A bench-owned mirror clone of the project's repository (made once, then refreshed by a fetch that runs
    inside the mirror only): the source repository, its .git and its state are only read."""
    dest = (base or home() / "mirrors") / slug(proj["name"])
    git = ["git", "-C", str(dest)]
    if dest.exists():
        r = subprocess.run([*git, "fetch", "--prune", "--quiet", "origin"], capture_output=True, text=True, timeout=600)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "--mirror", "--no-hardlinks", "--quiet", str(source or proj["root"]), str(dest)],
                           capture_output=True, text=True, timeout=1800)
    if r.returncode:
        raise RuntimeError(f"{proj['name']}: mirror {'fetch' if dest.exists() else 'clone'} failed: "
                           f"{(r.stderr or r.stdout).strip()[:300]}")
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
    flight and the reviews still to run may spend + this task's reservation fits the cap. A reservation is the
    worst case (every run spends its whole budget) until MEASURED_AFTER tasks have finished; then it is
    MEASURED_X times the mean actual spend of the finished ones (at least the floor, at most the worst case),
    so the cap buys the tasks it can really pay for. Actual spend still never passes the cap: a run's own
    budget stop bounds each cell."""

    def __init__(self, cap: float, spent: float = 0.0, review_mean: float | None = None):
        self.cap, self.spent, self.flight, self.reviews = cap, spent, 0.0, 0.0
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
            if self.spent + self.flight + self.reviews + worst + review > self.cap:
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
    rows = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            r = json.loads(line)
            rows[(r["task"], r["rep"], r["arm"])] = r
        except (ValueError, KeyError, TypeError):
            continue
    return rows


def append_row(path: Path, row: dict, lock: threading.Lock) -> None:
    with lock, open_append(path, "ab+") as f:
        torn = f.seek(0, 2) > 0 and f.seek(-1, 2) >= 0 and f.read(1) != b"\n"
        f.write((b"\n" if torn else b"") + json.dumps(row).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())


def run_task(out: Path, bench_dir: Path, task: dict, arms: dict, opts: dict, cfg: dict, ledger: Ledger,
             lock: threading.Lock) -> list[dict]:
    """The arms of one task, run one after the other (so a paired cell is never half run at the cap); each
    row goes to results.jsonl as it ends. A cell that already has a row is not run again."""
    path = out / "results.jsonl"
    have = read_rows(path)
    added = []
    for name, arm in arms.items():
        if (key := (task["task"], 0, name)) in have and not have[key].get("endpoint_fault"):
            continue
        cell = {"task": task["task"], "rep": 0, "arm": name}
        row = benchrun.run_cell(bench_dir, out, {**task, "task": task["task"]}, cell, arm,
                                {**opts, "repo": Path(task["source_root"]), "state": Path(task["state"])}, cfg)
        append_row(path, row, lock)
        added.append(row)
    return added


def run_cells(day: str, tasks: dict[str, dict], arms: dict, out: Path, bench_dir: Path, opts: dict, cfg: dict,
              cap: float, parallel: int = PARALLEL, order: list[str] | None = None, progress=None) -> dict:
    """Run the tasks' cells under the Claude cap. Returns {"ran": [task ids], "skipped": [{task, why}]}."""
    say = progress or (lambda s: None)
    out.mkdir(parents=True, exist_ok=True)
    rows = read_rows(out / "results.jsonl")
    spent = sum(float(r.get("cost_usd") or 0) for r in rows.values() if is_claude(arms.get(r["arm"], {})))
    spent += review_spent(out)
    ledger = Ledger(cap, spent, opts.get("review_mean"))
    n_claude = sum(is_claude(a) for a in arms.values())
    worst, review = n_claude * opts.get("budget_usd", CELL_USD), opts.get("review_usd", REVIEW_USD)
    lock = threading.Lock()
    ran, skipped, admitted = [], [], []
    todo = [t for t in (order or list(tasks)) if t in tasks]
    full = lambda t: all((t, 0, a) in rows and not rows[(t, 0, a)].get("endpoint_fault") for a in arms)
    ran += [t for t in todo if full(t)]
    todo = [t for t in todo if not full(t)]
    ledger.finished += [sum(float(rows[(t, 0, a)].get("cost_usd") or 0) for a in arms if is_claude(arms[a]))
                        for t in ran]   # a resumed run's finished tasks count toward the measured mean
    if ran and not (out / "review.json").exists():
        ledger.reviews += ledger.reservation(worst, review)[1] * len(ran)   # a resumed run's finished cells are not reviewed yet: their reviews still spend
    pending: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max(1, parallel)) as pool:
        def finish(f):
            tid, w = pending.pop(f)
            try:
                got = f.result()
            except Exception as e:   # one task's fault never stops the others
                say(f"{tid}: {type(e).__name__}: {e}")
                got = []
            ledger.done(w, sum(float(r.get("cost_usd") or 0) for r in got if is_claude(arms[r["arm"]])))
            ran.append(tid)
            say(f"{tid}: done; Claude spend ${ledger.total():.2f} of ${cap:.2f}")
        for tid in todo:
            while len(pending) >= parallel:   # a free slot first: a finished task's spend is then in the mean
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
                    skipped.append({"task": tid, "why": f"the ${cap:.0f} Claude cap leaves no room for another task "
                                                        f"(it would reserve ${hold + rev:.2f}; {rule})"})
                    break
                done, _ = concurrent.futures.wait(list(pending), return_when=concurrent.futures.FIRST_COMPLETED)
                for f in done:
                    finish(f)
            if not ok:
                continue
            admitted.append({"task": tid, "reserved_usd": round(hold + rev, 4), "rule": rule})
            say(f"{tid}: admitted, reserves ${hold + rev:.2f} ({rule})")
            f = pool.submit(run_task, out, bench_dir, tasks[tid], arms, opts, cfg, ledger, lock)
            pending[f] = (tid, hold)
        for f in list(pending):
            finish(f)
    return {"ran": ran, "skipped": skipped, "admitted": admitted, "claude_usd": ledger.total(), "reserved_reviews": ledger.reviews}


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
    (or stalled) is a fail whatever its diff shows."""
    if row.get("hang_guard") or row.get("outcome") in ("timeout", "stalled", "error", "runner_timeout"):
        return False
    s = score.get("scores") or {}
    return int(s.get("criteria", 0)) >= LANDED_MIN and int(s.get("correctness", 0)) >= LANDED_MIN


def second_reviewer(cfg: dict | None) -> dict | None:
    """The second blind grader (`bench.second_reviewer`: model, effort (default max), provider (default chat)),
    another model family than the first; None when unset. It grades every bench table's batch (the daily bench, the
    replay) again with the same masked items and rubric."""
    got = ((cfg or {}).get("bench") or {}).get("second_reviewer")
    if not isinstance(got, dict) or not isinstance(got.get("model"), str) or not got["model"].strip():
        return None
    return {"model": got["model"].strip(), "effort": str(got.get("effort") or "max"),
            "provider": str(got.get("provider") or "chat")}


def second_grade(out: Path, cfg: dict, ask2=None, rubric=benchblind.RUBRIC) -> dict | None:
    """Regrade the revealed batch in <out>/blind with the second reviewer into <out>/blind2 (resumable); returns
    {"reviewer", "scored": {item: (task, arm, score)}, "unscored", "compare", "review": spend} or None when there
    is none (no reviewer configured and no `ask2`, or no revealed batch). `ask2`: a reviewer callable for tests."""
    rv = second_reviewer(cfg)
    blind, blind2 = out / "blind", out / "blind2"
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
        prior = float(json.loads((out / "review2.json").read_text()).get("cost_usd") or 0)
    except (OSError, ValueError):
        pass
    durable_write(out / "review2.json", json.dumps({"cost_usd": round(prior + float(spend.get("cost_usd", 0)), 4),
                                                   "calls": spend.get("calls", 0)}))
    return {"reviewer": rv, "scored": {i: (by_item[i], labels[i], s) for i, s in b["scores"].items()},
            "unscored": unscored, "compare": benchblind.compare(blind, blind2), "review": spend}


def blind_review(out: Path, tasks: dict, arms: dict, cfg: dict, ask=None, seed: int | None = None,
                 build=None, rubric=benchblind.RUBRIC, ask2=None) -> dict:
    """Seal the finished cells, have the Opus reviewer score each blind, and return {item: (task, arm, score)}
    once revealed; an item the reviewer could not score stays out of the table. `ask`: a reviewer
    callable for tests. `build(out, tasks, arms)` and `rubric`: another kind of cell and its rubric (the
    offline replay's answers); the default builds the diff cells. With a second reviewer (`bench.second_reviewer`,
    or `ask2`) the revealed batch is graded again, and the result's "second" holds that grading."""
    blind = out / "blind"
    spend: dict = {}
    if not blind.exists():
        built = build(out, tasks, arms) if build else benchrun.blind_cells(out, tasks, arms, [{"rep": 0}])
        if not built["cells"]:
            return {"scored": {}, "unscored": [], "review": spend}
        durable_write(out / "cells.json", json.dumps(built, indent=1) + "\n")
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
    prior = review_spent(out)
    durable_write(out / "review.json", json.dumps({"cost_usd": round(prior + float(spend.get("cost_usd", 0)), 4),
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
           "review": spend}
    if b["phase"] == "revealed" and (sec := second_grade(out, cfg, ask2, rubric)):
        got["second"] = sec
    return got


def day_record(day: str, tasks: dict, arms: dict, out: Path, graded: dict, skipped: list, dropped: list) -> dict:
    """The day's rows, one per task and arm that was reviewed: project, scores, landed, cost, minutes."""
    rows = read_rows(out / "results.jsonl")
    cells = []
    for item, (tid, arm, score) in sorted(graded["scored"].items()):
        r = rows.get((tid, 0, arm)) or {}
        t = tasks.get(tid) or {}
        cells.append({"project": t.get("project", ""), "task": tid, "source_task": t.get("source_task"), "arm": arm,
                      "model": f"{arms[arm].get('provider')}:{arms[arm].get('model')}:{arms[arm].get('effort')}"
                               + (f":{arms[arm]['harness']}" if arms[arm].get("harness") else ""),
                      "claude": is_claude(arms[arm]),
                      "criteria": int(score["scores"]["criteria"]), "correctness": int(score["scores"]["correctness"]),
                      "tests": int(score["scores"].get("tests", 0)), "scope": int(score["scores"].get("scope", 0)),
                      "landed": landed(score, r), "cost_usd": float(r.get("cost_usd") or 0),
                      "minutes": round(float(r.get("wall_s") or 0) / 60, 2),
                      "stopped": bool(r.get("hang_guard")) or r.get("outcome") in ("timeout", "stalled"),
                      "outcome": r.get("outcome", "")})
    sec = graded.get("second")
    if sec:
        two = {(t, a): sc for t, a, sc in sec["scored"].values()}
        for c in cells:
            sc = (two.get((c["task"], c["arm"])) or {}).get("scores")
            if sc:
                c["second_criteria"], c["second_correctness"] = int(sc["criteria"]), int(sc["correctness"])
                c["second_landed"] = landed({"scores": sc}, rows.get((c["task"], 0, c["arm"])) or {})
    claude_cells = sum(c["cost_usd"] for c in cells if c["claude"])
    return {"day": day, "cells": cells, "skipped": skipped, "dropped": dropped,
            "unscored": graded.get("unscored", []),
            **({"second_grader": {"reviewer": sec["reviewer"], "compare": sec["compare"],
                                  "unscored": sec["unscored"]}} if sec else {}),
            "spend": {"claude_usd": round(claude_cells + review_spent(out), 4),
                      "claude_arms_usd": round(claude_cells, 4), "review_usd": round(review_spent(out), 4),
                      "glm_usd": round(sum(c["cost_usd"] for c in cells if not c["claude"]), 4)}}


# -- cumulative table

def cumulative(root: Path | None = None) -> dict:
    """The running table from every day's record under `root`/days: per project and arm, and per arm overall."""
    root = root or home()
    cells, gaps, seconds = [], [], []
    days = sorted((root / "days").glob("*.json")) if (root / "days").exists() else []
    for p in days:
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        cells += rec.get("cells") or []
        if (cmp := (rec.get("second_grader") or {}).get("compare")):
            seconds.append({"day": p.stem, "ranking_changed": cmp["ranking_changed"], "rank_a": cmp["rank_a"],
                            "rank_b": cmp["rank_b"]})
        if rec.get("faults") or rec.get("skipped"):
            gaps.append({"day": p.stem, "faults": rec.get("faults") or [], "skipped": len(rec.get("skipped") or [])})

    def agg(rows: list[dict]) -> dict:
        n = len(rows)
        return {"n": n, "landed": sum(1 for r in rows if r["landed"]),
                "criteria": round(sum(r["criteria"] for r in rows) / n, 2),
                "correctness": round(sum(r["correctness"] for r in rows) / n, 2),
                "usd_per_task": round(sum(r["cost_usd"] for r in rows) / n, 2),
                "min_per_task": round(sum(r["minutes"] for r in rows) / n, 1),
                "stops": sum(1 for r in rows if r["stopped"]),
                **({"second_n": len(two), "second_landed": sum(1 for r in two if r["second_landed"]),
                    "second_criteria": round(sum(r["second_criteria"] for r in two) / len(two), 2),
                    "second_correctness": round(sum(r["second_correctness"] for r in two) / len(two), 2)}
                   if (two := [r for r in rows if "second_landed" in r]) else {})} if n else {}
    by, per = {}, {}
    for c in cells:
        by.setdefault((c["project"], c["model"]), []).append(c)
        per.setdefault(c["model"], []).append(c)
    return {"days": [p.stem for p in days],
            "projects": {proj: {m: agg(rs) for (pr, m), rs in sorted(by.items()) if pr == proj}
                         for proj in sorted({c["project"] for c in cells})},
            "models": {m: agg(rs) for m, rs in sorted(per.items())}, "gaps": gaps,
            **({"second_days": seconds} if seconds else {})}


def _row(label: str, a: dict, second: bool = False) -> str:
    tail = (f" {a['second_landed']}/{a['second_n']} ({a['second_criteria']}/{a['second_correctness']}) |"
            if a.get("second_n") else " |") if second else ""
    return (f"| {label} | {a['n']} | {a['landed']}/{a['n']} | {a['criteria']} | {a['correctness']} | "
            f"${a['usd_per_task']} | {a['min_per_task']} | {a['stops']} |{tail}")


HEAD = ("| {} | N | landed | requirements (0-4) | correctness (0-4) | $ per task | minutes per task | 60-min stops |{}\n"
        "|---|---|---|---|---|---|---|---|{}")


def table_md(cum: dict) -> str:
    """The cumulative table as markdown: per model overall, then per project. A second grader's cells add a
    column: landed of those it graded (mean requirements/correctness)."""
    two = any(a.get("second_n") for a in cum["models"].values())
    head = HEAD.format("model", " second grader: landed (req/corr) |" if two else "", "---|" if two else "")
    lines = [f"# Daily model bench: cumulative ({len(cum['days'])} day(s): {', '.join(cum['days']) or 'none'})", "",
             "Landed = requirements and correctness each at least 3 of 4 by the blind Opus review; a 60-minute stop is "
             "a fail. $ per task is each arm's own spend (an open model's is GLM spend, not Claude spend).", "",
             "## Per model", "", head]
    lines += [_row(m, a, two) for m, a in cum["models"].items()]
    for proj, ms in cum["projects"].items():
        lines += ["", f"## {proj}", "", head]
        lines += [_row(m, a, two) for m, a in ms.items()]
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


def run_day(day: str, start: float, end: float, arms: dict, cfg: dict, cap: float = CAP_USD, parallel: int = PARALLEL,
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
    projs = {p["name"]: p for p in projects(skip)}
    tasks: dict[str, dict] = {}
    for name in sorted({k["project"] for k in picked["order"]}):
        repo = root / "mirrors" / slug(name)
        tasks.update(build(day, projs[name], [k for k in picked["order"] if k["project"] == name], repo, bench_dir))
    order = [f"{slug(k['project'])}-{k['task']}" for k in picked["order"]]
    opts = {"timeout_s": LIMIT_S, "stall_s": 900, "budget_usd": CELL_USD, "review_usd": REVIEW_USD,
            "test_cmd": benchrun.TEST_CMD, "test_timeout_s": 900, "dry_run": False, "scratch_home": True,
            "review_mean": prior_review_mean(root, day), **(opts or {})}
    done = run_cells(day, tasks, arms, out, bench_dir, opts, cfg, cap, parallel, order, say)
    graded = blind_review(out, tasks, arms, cfg, ask, ask2=ask2)
    skipped = picked.get("skipped", []) + done["skipped"]
    rec = day_record(day, tasks, arms, out, graded, skipped, picked["dropped"])
    rec["faults"] = picked.get("faults", [])
    rec["admitted"] = done["admitted"]   # which reservation rule admitted each task
    rec["arms"] = {k: {f: v.get(f, "") for f in ("provider", "model", "effort", "harness")} for k, v in arms.items()}
    durable_write(root / "days" / f"{day}.json", json.dumps(rec, indent=1) + "\n")
    write_table(root)
    return rec


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
