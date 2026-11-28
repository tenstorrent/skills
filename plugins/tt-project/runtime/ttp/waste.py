# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The waste ledger (`ttp waste`, the daily review): this project's avoidable spend, model-free, from
its own runs. Each run that ended in the window counts once, in the first bucket it fits:

  stand_in       runs started after the project moved (kv `project_moved_at` or `waste.moved_at`, epoch):
                 the old copy kept working beside the new one
  superseded     work on tasks later cancelled, superseded (task_superseded) or abandoned
  failed_owner   work on failed tasks that kept something: a commit or passed checks in one of its runs, a
                 passing review of it, or a successor that `continues` it. All of it is retained, its evidence
                 listed per task (the run receipts, the continuation and review ids)
  failed_task    work on tasks that failed with no verdict and kept none of that: verified discarded work
  rejected       failed reviews that returned a verdict (changes_needed, superseded, or a failed hand-off with
                 review_failure or followups): rework of the change they reviewed, listed by that change,
                 not a broken run
  duplicate      a task whose title an earlier task of the last 7 days already had (schedules aside)
  rereview       a review of a head an earlier review already checked (same review_head label and
                 same hand-off body: review_payload label; a review without one is unknown, not waste)
  review_rounds  reviews past the `waste.review_rounds`th round of one change's chain
  crashed        crashed, stalled or timed-out runs (budget.wasted) on tasks that later finished
  over_runs      a task's runs past its `waste.max_runs`th that brought no new result. One that did (a done or
                 waiting hand-off that is not its previous run's again, or a commit inside it: the run stop's
                 runcap.committed) is a continuation: listed apart with its rank and the limit, not avoidable.
                 A repeated hand-off, a self-wait's poll too, is no new result
  costly_wake    the part of a coordinator wake over `waste.wake_usd`, unless the host slept SLEPT_COLD_S since
                 the wake before (no prompt cache outlives that: its cold rewrite is the sleep's)

The total is every run's booking in the window; the share is avoidable less retained, over total.

Superseded work a later task kept is still counted there, gross, and also shown apart as retained: a
cancelled task another task `continues` (its branch, packets or evidence carry on), work re-landed as
another commit (task_superseded) or a correction class a later fix closed (task_class_fix). Retained
spend is neither saved nor a loss to fix: the share, the target check and the fix bucket all use spend less
retained. The ledger
only reads; it never closes a task, an obligation or a promise."""
from __future__ import annotations

import re
from pathlib import Path

from . import budget, chainprio, runcap
from .db import CHANGES_NEEDED, load_result
from .reviewreuse import FORCED_LABEL, HEAD_LABEL, PAYLOAD_LABEL
from .util import task_labels

BUCKETS = ("stand_in", "superseded", "failed_owner", "failed_task", "rejected", "duplicate", "rereview", "review_rounds", "crashed",
           "over_runs", "costly_wake")
LABELS = {"stand_in": "stand-in after a move", "superseded": "cancelled or superseded work",
          "failed_owner": "failed tasks whose work was kept",
          "failed_task": "failed tasks", "rejected": "rejected changes", "duplicate": "duplicate tasks", "rereview": "re-reviews of a same head",
          "review_rounds": "review rounds past the limit", "crashed": "crashed or timed-out runs",
          "over_runs": "runs past the per-task limit", "costly_wake": "coordinator wakes over the limit"}
DEFAULTS = {"target_share": 0.10, "wake_usd": 1.0, "max_runs": 4, "review_rounds": 2, "fix_task": True}
DUP_WINDOW_S = 7 * 86400
SLEPT_COLD_S = 3600      # the longest prompt cache life (1h)
WAKE_FIX_MIN_USD = 0.5   # one costly wake less over the limit than this queues no fix task


def settings(cfg: dict | None) -> dict:
    got = dict(DEFAULTS)
    for k, v in ((cfg or {}).get("waste") or {}).items():
        got[k] = v
    return got


def _num(labels: list[str], prefix: str) -> int | None:
    for v in labels:
        if v.startswith(prefix) and v[len(prefix):].isdigit():
            return int(v[len(prefix):])
    return None


def _norm(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _moved_at(db, cfg: dict) -> float:
    for v in (db.kv("project_moved_at", None), cfg.get("moved_at")):
        try:
            if v not in (None, "") and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    return 0.0


def _verdict(task: dict) -> bool:
    """A failed review that returned a verdict on the change: its spend is rework of that change. Read from
    the hand-off reviewsup keeps as it was, so a superseded crashed run stays a failed task."""
    result = load_result(task["result"])
    return result.get("status") == CHANGES_NEEDED or (
        result.get("status") == "failed" and bool(result.get("review_failure") or result.get("followups")))


def _task_sets(db, cfg: dict) -> dict:
    tasks = {t["id"]: t for t in db.q("SELECT id, created, title, kind, status, origin, labels FROM tasks")}
    gone = {r["task"] for r in db.q("SELECT task FROM task_superseded UNION SELECT task FROM task_abandoned")}
    rejected = {r["id"]: (chainprio.covered(r) or [r["id"]])[0]           # the review -> the change it reviewed
                for r in db.q("SELECT id, kind, status, result, labels FROM tasks WHERE status='failed' AND kind='review'")
                if _verdict(r)}
    out: dict[str, set[int]] = {
        "superseded": {i for i, t in tasks.items() if t["status"] == "cancelled"} | gone,
        "failed_task": {i for i, t in tasks.items() if t["status"] == "failed"} - set(rejected), "rejected": set(rejected),
        "rejected_of": rejected,
        "done": {i for i, t in tasks.items() if t["status"] == "done"},
        "duplicate": set(), "rereview": set(), "review_rounds": set(), "retained": set(),
        "continued_by": {}, "accepted_by": {}}
    kept = {r["task"] for r in db.q("SELECT task FROM task_superseded UNION "
                                    "SELECT task FROM task_class_fix WHERE fix_task IS NOT NULL OR sha != ''")}
    seen: dict[str, float] = {}
    heads: set[tuple[str, str]] = set()
    reviews: dict[int, list[str]] = {}
    for i in sorted(tasks):
        t = tasks[i]
        labels = task_labels(t["labels"])
        prior = _num(labels, "continues:")
        if prior in out["superseded"] and t["status"] not in ("cancelled", "failed") and i not in gone:
            kept.add(prior)   # a successor carries its work on
        if prior in out["failed_task"] and t["status"] not in ("cancelled", "failed"):
            out["continued_by"].setdefault(prior, []).append(i)
        if t["origin"] != "schedule" and t["kind"] != "review" and _num(labels, "continues:") is None:
            key, at = _norm(t["title"]), float(t["created"] or 0)
            if key and key in seen and at - seen[key] <= DUP_WINDOW_S:
                out["duplicate"].add(i)
            seen[key] = at
        if t["kind"] == "review":
            reviews[i] = labels
            if t["status"] == "done":
                for c in chainprio.covered(t):
                    out["accepted_by"].setdefault(c, []).append(i)
            head = next((v for v in labels if v.startswith(HEAD_LABEL)), "")
            payload = next((v for v in labels if v.startswith(PAYLOAD_LABEL)), "")
            if head and payload and FORCED_LABEL not in labels:
                if (head, payload) in heads:
                    out["rereview"].add(i)
                heads.add((head, payload))
    limit = int(cfg.get("review_rounds") or 0)
    for i, labels in reviews.items():
        depth, cur, walked = 1, labels, {i}
        while (prev := _num(cur, "continues:")) in reviews and prev not in walked:
            walked.add(prev)
            depth, cur = depth + 1, reviews[prev]
        if limit and depth > limit:
            out["review_rounds"].add(i)
    out["retained"] = kept & out["superseded"]
    return out


def _kept(db, p, task: int, sets: dict) -> list[str]:
    """The evidence failed task `task` kept work, empty for verified discarded work: its successors, its
    passing reviews, and the runs that committed (runcap.committed) or passed their own checks (checks.rc 0)."""
    from .cli import CHECKS_RC
    out = [f"continued by #{i}" for i in sets["continued_by"].get(task, [])]
    out += [f"passed review #{i}" for i in sets["accepted_by"].get(task, [])]
    row = db.one("SELECT * FROM tasks WHERE id=?", (task,)) or {}
    for r in db.q("SELECT id, dir, started, ended FROM runs WHERE task=? AND ended IS NOT NULL ORDER BY id", (task,)):
        try:
            passed = bool(r["dir"]) and (Path(r["dir"]) / CHECKS_RC).read_text().strip() == "0"
        except OSError:
            passed = False
        if passed:
            out.append(f"run {r['id']} passed its checks")
        elif p is not None and runcap.committed(p, row, r) is True:
            out.append(f"run {r['id']} committed")
    return out


def _slept_cold(db, r, sleeps: list) -> bool:
    """The host slept at least SLEPT_COLD_S between coordinator wake `r` and the one before it."""
    if not sleeps:
        return False
    prev = db.one("SELECT MAX(ended) AS e FROM runs WHERE (task IS NULL OR role='coordinator') AND id != ? "
                  "AND ended <= ?", (r["id"], r["started"]))
    start, since = float(r["started"] or 0), float((prev and prev["e"]) or 0)
    return sum(max(0.0, min(b, start) - max(a, since)) for a, b in sleeps) >= SLEPT_COLD_S


def _result_key(h: dict) -> tuple:
    return h.get("status"), " ".join(str(h.get("summary") or "").split())


def _new_result(db, p, r: dict, prev: dict | None) -> bool:
    """Run `r`, past the per-task limit, brought a new result: a done or waiting hand-off that is not its
    previous run's again, or a commit inside it (runcap.committed). Unknown evidence is no result."""
    h = runcap.handoff(r)
    if h.get("status") in runcap.PROGRESS and _result_key(h) != _result_key(runcap.handoff(prev) if prev else {}):
        return True
    return runcap.committed(p, db.one("SELECT * FROM tasks WHERE id=?", (r["task"],)) or {}, r) is True


def report(db, since: float, now: float, cfg: dict | None = None, p=None) -> dict:
    """{total, avoidable, retained, share, buckets: {name: {usd, runs, run_ids, tasks, retained_usd,
    retained_tasks}}, continued: {usd, runs: [{run, task, rank}], limit}} for runs that ended in [since, now).
    `p` lets over_runs read a code run's commits; without it a run with no done or waiting hand-off stays
    avoidable."""
    cfg = settings(cfg)
    runs = db.q("SELECT id, task, role, dir, started, ended, status, note, cost_usd FROM runs "
                "WHERE ended IS NOT NULL AND ended >= ? AND ended < ? ORDER BY id", (since, now))
    sets = _task_sets(db, cfg) if runs else {}
    moved = _moved_at(db, cfg)
    max_runs = int(cfg.get("max_runs") or 0)
    rank: dict[int, int] = {}
    count: dict[int, int] = {}
    prev: dict[int, dict] = {}
    last: dict[int, dict] = {}
    ids = sorted({r["task"] for r in runs if r["task"] is not None})
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in db.q(f"SELECT id, task, dir FROM runs WHERE task IN ({','.join('?' * len(part))}) "
                      f"AND status NOT IN ({','.join('?' * len(runcap.FREE))}) AND {runcap.NOT_CARRY_OK} ORDER BY started, id",
                      (*part, *runcap.FREE)):   # as the run stop counts: one that never reached the model is no run
            count[r["task"]] = rank[r["id"]] = count.get(r["task"], 0) + 1
            if r["task"] in last:
                prev[r["id"]] = last[r["task"]]
            last[r["task"]] = r
    wake = float(cfg.get("wake_usd") or 0)
    sleeps = db.kv("host_sleeps", []) or [] if wake else []   # the daemon's record of this host's sleeps
    buckets = {b: {"usd": 0.0, "runs": 0, "run_ids": [], "tasks": {}, "retained_usd": 0.0, "retained_tasks": {}}
               for b in BUCKETS}
    evidence: dict[int, list[str]] = {}   # failed task -> what it kept
    continued = {"usd": 0.0, "runs": [], "limit": max_runs}
    total = 0.0
    for r in runs:
        cost, task = float(r["cost_usd"] or 0), r["task"]
        total += cost
        usd, got = cost, None
        if moved and float(r["started"] or 0) >= moved:
            got = "stand_in"
        elif task is None or r["role"] == "coordinator":
            if wake and cost > wake and not _slept_cold(db, r, sleeps):
                got, usd = "costly_wake", cost - wake
        elif task in sets["superseded"]:
            got = "superseded"
        elif task in sets["failed_task"]:
            if task not in evidence:
                evidence[task] = _kept(db, p, task, sets)
            got = "failed_owner" if evidence[task] else "failed_task"
        elif task in sets["rejected"]:
            got = "rejected"
        elif task in sets["duplicate"]:
            got = "duplicate"
        elif task in sets["rereview"]:
            got = "rereview"
        elif task in sets["review_rounds"]:
            got = "review_rounds"
        elif task in sets["done"] and budget.wasted(r):
            got = "crashed"
        elif max_runs and rank.get(r["id"], 0) > max_runs and cost > 0:
            if _new_result(db, p, r, prev.get(r["id"])):
                continued["usd"] += cost
                continued["runs"].append({"run": r["id"], "task": task, "rank": rank[r["id"]]})
            else:
                got = "over_runs"
        if got and usd > 0:
            b = buckets[got]
            b["usd"] += usd
            b["runs"] += 1
            b["run_ids"].append(r["id"])
            if task is not None:
                named = sets["rejected_of"][task] if got == "rejected" else task
                b["tasks"][named] = b["tasks"].get(named, 0.0) + usd
            if got == "failed_owner" or (got == "superseded" and task in sets["retained"]):
                b["retained_usd"] += usd
                b["retained_tasks"][task] = b["retained_tasks"].get(task, 0.0) + usd
    for b in buckets.values():
        for k in ("tasks", "retained_tasks"):
            b[k] = [t for t, _ in sorted(b[k].items(), key=lambda x: -x[1])]
    avoidable = sum(b["usd"] for b in buckets.values())
    retained = sum(b["retained_usd"] for b in buckets.values())
    return {"total": total, "avoidable": avoidable, "retained": retained,
            "share": max(avoidable - retained, 0.0) / total if total else 0.0,
            "target": float(cfg.get("target_share") or 0), "buckets": buckets, "continued": continued,
            "kept": {t: evidence[t] for t in buckets["failed_owner"]["tasks"]}}


def lines(rep: dict) -> list[str]:
    """One line per bucket with spend: $, share of the total, runs and the costliest tasks."""
    out = []
    for name in sorted(BUCKETS, key=lambda n: -rep["buckets"][n]["usd"]):
        b = rep["buckets"][name]
        if b["usd"] <= 0:
            continue
        share = b["usd"] / rep["total"] if rep["total"] else 0.0
        top = ", ".join(f"#{t}" for t in b["tasks"][:5])
        kept = ", ".join(f"#{t}" for t in b["retained_tasks"][:5])
        out.append(f"{LABELS[name]}: ${b['usd']:.2f} ({share:.0%}), {b['runs']} run(s)" + (f"; {top}" if top else "")
                   + (f"; ${b['retained_usd']:.2f} of it kept by a successor's work ({kept}), not saved"
                      if b["retained_usd"] > 0 and name != "failed_owner" else "")
                   + ("; kept, not waste: " + "; ".join(kept_line(rep)) if name == "failed_owner" else ""))
    c = rep.get("continued") or {}
    if c.get("runs"):
        named = ", ".join(f"run {x['run']} (#{x['task']}, run {x['rank']})" for x in c["runs"][:5])
        out.append(f"runs past the per-task limit ({c['limit']}) that brought a new result: ${c['usd']:.2f}, "
                   f"{len(c['runs'])} run(s), not avoidable; {named}")
    return out


def kept_line(rep: dict, limit: int = 5) -> list[str]:
    """Per failed task whose work was kept, its evidence: `#12 (continued by #15, run 40 committed)`."""
    return [f"#{t} ({', '.join(e)})" for t, e in list((rep.get("kept") or {}).items())[:limit]]


def over(rep: dict) -> bool:
    return rep["total"] > 0 and rep["share"] > rep["target"]


def line(project: str, rep: dict) -> str:
    """The one short daily line for the user's chat: the gross avoidable figure, the part successors kept
    apart, and the share net of it."""
    top = fix_bucket(rep, any_share=True)
    kept = rep.get("retained", 0) > 0
    return (f"{project}: avoidable ${rep['avoidable']:.2f} of ${rep['total']:.2f}"
            + (f", ${rep['retained']:.2f} of it kept by successors" if kept else "")
            + f" ({'net ' if kept else ''}{rep['share']:.0%})"
            + (f", most in {LABELS[top]}" if top else "")
            + (f"; over the {rep['target']:.0%} target" if over(rep) else ""))


def fix_bucket(rep: dict, any_share: bool = False) -> str:
    """The largest bucket, when the avoidable share is over target (or always, with any_share). Without
    any_share (the fix task's pick), one costly wake under WAKE_FIX_MIN_USD over the limit picks none."""
    if not (any_share or over(rep)):
        return ""
    net = {n: rep["buckets"][n]["usd"] - rep["buckets"][n].get("retained_usd", 0.0) for n in BUCKETS}
    best = max(BUCKETS, key=lambda n: net[n])
    if not any_share and best == "costly_wake" and rep["buckets"][best]["runs"] == 1 \
            and net[best] < WAKE_FIX_MIN_USD:
        return ""
    return best if net[best] > 0 else ""
