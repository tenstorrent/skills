# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The waste ledger (`ttp waste`, the daily review): this project's avoidable spend, model-free, from
its own runs. Each run that ended in the window counts once, in the first bucket it fits:

  stand_in       runs started after the project moved (kv `project_moved_at` or `waste.moved_at`, epoch):
                 the old copy kept working beside the new one
  superseded     work on tasks later cancelled, superseded (task_superseded) or abandoned
  failed_task    work on tasks that failed
  duplicate      a task whose title an earlier task of the last 7 days already had (schedules aside)
  rereview       a review of a head an earlier review already checked (same review_head label)
  review_rounds  reviews past the `waste.review_rounds`th round of one change's chain
  crashed        crashed, stalled or timed-out runs (budget.wasted) on tasks that later finished
  over_runs      a task's runs past its `waste.max_runs`th
  costly_wake    the part of a coordinator wake over `waste.wake_usd`

The total is every run's booking in the window; the share is avoidable over total."""
from __future__ import annotations

import json
import re
from typing import Any

from . import budget
from .reviewreuse import FORCED_LABEL, HEAD_LABEL

BUCKETS = ("stand_in", "superseded", "failed_task", "duplicate", "rereview", "review_rounds", "crashed",
           "over_runs", "costly_wake")
LABELS = {"stand_in": "stand-in after a move", "superseded": "cancelled or superseded work",
          "failed_task": "failed tasks", "duplicate": "duplicate tasks", "rereview": "re-reviews of a same head",
          "review_rounds": "review rounds past the limit", "crashed": "crashed or timed-out runs",
          "over_runs": "runs past the per-task limit", "costly_wake": "coordinator wakes over the limit"}
DEFAULTS = {"target_share": 0.10, "wake_usd": 1.0, "max_runs": 4, "review_rounds": 2, "fix_task": True}
DUP_WINDOW_S = 7 * 86400


def settings(cfg: dict | None) -> dict:
    got = dict(DEFAULTS)
    for k, v in ((cfg or {}).get("waste") or {}).items():
        got[k] = v
    return got


def _labels(raw: Any) -> list[str]:
    try:
        got = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return [str(x) for x in got] if isinstance(got, list) else []


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


def _task_sets(db, cfg: dict) -> dict[str, set[int]]:
    tasks = {t["id"]: t for t in db.q("SELECT id, created, title, kind, status, origin, labels FROM tasks")}
    gone = {r["task"] for r in db.q("SELECT task FROM task_superseded UNION SELECT task FROM task_abandoned")}
    out: dict[str, set[int]] = {
        "superseded": {i for i, t in tasks.items() if t["status"] == "cancelled"} | gone,
        "failed_task": {i for i, t in tasks.items() if t["status"] == "failed"},
        "done": {i for i, t in tasks.items() if t["status"] == "done"},
        "duplicate": set(), "rereview": set(), "review_rounds": set()}
    seen: dict[str, float] = {}
    heads: set[str] = set()
    reviews: dict[int, list[str]] = {}
    for i in sorted(tasks):
        t = tasks[i]
        labels = _labels(t["labels"])
        if t["origin"] != "schedule" and t["kind"] != "review" and _num(labels, "continues:") is None:
            key, at = _norm(t["title"]), float(t["created"] or 0)
            if key and key in seen and at - seen[key] <= DUP_WINDOW_S:
                out["duplicate"].add(i)
            seen[key] = at
        if t["kind"] == "review":
            reviews[i] = labels
            head = next((v for v in labels if v.startswith(HEAD_LABEL)), "")
            if head and FORCED_LABEL not in labels:
                if head in heads:
                    out["rereview"].add(i)
                heads.add(head)
    limit = int(cfg.get("review_rounds") or 0)
    for i, labels in reviews.items():
        depth, cur, walked = 1, labels, {i}
        while (prev := _num(cur, "continues:")) in reviews and prev not in walked:
            walked.add(prev)
            depth, cur = depth + 1, reviews[prev]
        if limit and depth > limit:
            out["review_rounds"].add(i)
    return out


def report(db, since: float, now: float, cfg: dict | None = None) -> dict:
    """{total, avoidable, share, buckets: {name: {usd, runs, tasks}}} for runs that ended in [since, now)."""
    cfg = settings(cfg)
    runs = db.q("SELECT id, task, role, started, ended, status, note, cost_usd FROM runs "
                "WHERE ended IS NOT NULL AND ended >= ? AND ended < ? ORDER BY id", (since, now))
    sets = _task_sets(db, cfg) if runs else {}
    moved = _moved_at(db, cfg)
    max_runs = int(cfg.get("max_runs") or 0)
    rank: dict[int, int] = {}
    count: dict[int, int] = {}
    ids = sorted({r["task"] for r in runs if r["task"] is not None})
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in db.q(f"SELECT id, task FROM runs WHERE task IN ({','.join('?' * len(part))}) "
                      "ORDER BY started, id", part):
            count[r["task"]] = rank[r["id"]] = count.get(r["task"], 0) + 1
    wake = float(cfg.get("wake_usd") or 0)
    buckets = {b: {"usd": 0.0, "runs": 0, "tasks": {}} for b in BUCKETS}
    total = 0.0
    for r in runs:
        cost, task = float(r["cost_usd"] or 0), r["task"]
        total += cost
        usd, got = cost, None
        if moved and float(r["started"] or 0) >= moved:
            got = "stand_in"
        elif task is None or r["role"] == "coordinator":
            if wake and cost > wake:
                got, usd = "costly_wake", cost - wake
        elif task in sets["superseded"]:
            got = "superseded"
        elif task in sets["failed_task"]:
            got = "failed_task"
        elif task in sets["duplicate"]:
            got = "duplicate"
        elif task in sets["rereview"]:
            got = "rereview"
        elif task in sets["review_rounds"]:
            got = "review_rounds"
        elif task in sets["done"] and budget.wasted(r):
            got = "crashed"
        elif max_runs and rank.get(r["id"], 0) > max_runs:
            got = "over_runs"
        if got and usd > 0:
            b = buckets[got]
            b["usd"] += usd
            b["runs"] += 1
            if task is not None:
                b["tasks"][task] = b["tasks"].get(task, 0.0) + usd
    for b in buckets.values():
        b["tasks"] = [t for t, _ in sorted(b["tasks"].items(), key=lambda x: -x[1])]
    avoidable = sum(b["usd"] for b in buckets.values())
    return {"total": total, "avoidable": avoidable, "share": avoidable / total if total else 0.0,
            "target": float(cfg.get("target_share") or 0), "buckets": buckets}


def lines(rep: dict) -> list[str]:
    """One line per bucket with spend: $, share of the total, runs and the costliest tasks."""
    out = []
    for name in sorted(BUCKETS, key=lambda n: -rep["buckets"][n]["usd"]):
        b = rep["buckets"][name]
        if b["usd"] <= 0:
            continue
        share = b["usd"] / rep["total"] if rep["total"] else 0.0
        top = ", ".join(f"#{t}" for t in b["tasks"][:5])
        out.append(f"{LABELS[name]}: ${b['usd']:.2f} ({share:.0%}), {b['runs']} run(s)" + (f"; {top}" if top else ""))
    return out


def over(rep: dict) -> bool:
    return rep["total"] > 0 and rep["share"] > rep["target"]


def line(project: str, rep: dict) -> str:
    """The one short daily line for the user's chat."""
    top = fix_bucket(rep, any_share=True)
    return (f"{project}: avoidable ${rep['avoidable']:.2f} of ${rep['total']:.2f} ({rep['share']:.0%})"
            + (f", most in {LABELS[top]}" if top else "")
            + (f"; over the {rep['target']:.0%} target" if over(rep) else ""))


def fix_bucket(rep: dict, any_share: bool = False) -> str:
    """The largest bucket, when the avoidable share is over target (or always, with any_share)."""
    if not (any_share or over(rep)):
        return ""
    best = max(BUCKETS, key=lambda n: rep["buckets"][n]["usd"])
    return best if rep["buckets"][best]["usd"] > 0 else ""
