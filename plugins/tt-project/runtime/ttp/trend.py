# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The trend of a task chain's metrics across its runs, for the web app's task rows.

A chain is a task with its `continues:` successors, fix and re-review tasks (reviewcap.links without
`parent`, so a plan that spawned many follow-ups does not join them all). Each run's hand-off may carry
`metrics` {name: value}; numeric values (a number, or a number and a unit such as "12.5 ms") form one
series per name, oldest first, with each point's run, time in the project's home zone and delta.
Hashes and ids are not metrics: a value that looks like a commit hash, or a name such as
reviewed_head, pushed_sha or task_id, is skipped. A series has one unit: points with another unit are
dropped. A series needs at least two points: a chain without one gets no trend.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import timefmt
from .db import DB, load_result
from .reviewcap import links

LOOKBACK_S = 14 * 86400   # tasks this recent are searched for chain links
MAX_POINTS = 30           # the latest points of a series
MAX_SERIES = 4            # the series with the most points
MAX_CACHE = 5000
NUMBER = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z%µ][A-Za-z%µ/]{0,11})?\s*$")
HEX = re.compile(r"^[0-9a-fA-F]+$")
ID_NAME = re.compile(r"^(reviewed_head|pushed|rebased_)|commit|(^|_)(sha|head|id)$|_range$", re.I)

_cache: dict[int, list] = {}   # ended run id -> its numeric metrics; a hand-off never changes after


def numeric(value) -> tuple[float, str] | None:
    """(number, unit) for a metric value that is a number or a number with a unit, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return (float(value), "") if value == value else None
    if isinstance(value, str) and (m := NUMBER.match(value)):
        compact = value.strip()
        if m.group(2) and HEX.match(compact) or 7 <= len(compact) <= 40 and HEX.match(compact) and not compact.isdigit():
            return None   # a commit hash such as 4ea0610c2d90 or 1e5abc, not a number with a unit
        return float(m.group(1)), m.group(2) or ""
    return None


def metrics_of(result: dict) -> list[tuple[str, float, str]]:
    """The numeric metrics of a hand-off, as (name, value, unit)."""
    m = result.get("metrics")
    if not isinstance(m, dict):
        return []
    out = []
    for name, v in m.items():
        if not ID_NAME.search(str(name)) and (n := numeric(v)) is not None:
            out.append((str(name)[:60], n[0], n[1][:20]))
    return out


def _run_metrics(run: dict) -> list:
    if run["id"] in _cache:
        return _cache[run["id"]]
    f = Path(run["dir"]) / "result.json" if run.get("dir") else None
    try:
        got = metrics_of(load_result(f.read_text(errors="replace"))) if f and f.is_file() else None
    except OSError:
        got = None
    if got is not None and run.get("status") != "running":
        if len(_cache) > MAX_CACHE:
            _cache.clear()
        _cache[run["id"]] = got
    return got or []


def chains(db: DB, ids, now: float | None = None) -> dict[int, list[int]]:
    """{task id: the ids of its chain, sorted} for each of `ids`."""
    now = time.time() if now is None else now
    ids = {int(i) for i in ids}
    rows = db.q("SELECT id,kind,title,labels,parent,depends_on FROM tasks WHERE created>? OR updated>?",
                (now - LOOKBACK_S, now - LOOKBACK_S))
    up: dict[int, int] = {}

    def find(i: int) -> int:
        while up.setdefault(i, i) != i:
            up[i] = up[up[i]]
            i = up[i]
        return i
    for t in rows:
        for o in links({**t, "parent": None}):
            up[find(t["id"])] = find(o)
    groups: dict[int, list[int]] = {}
    for i in list(up):
        groups.setdefault(find(i), []).append(i)
    return {i: sorted(groups.get(find(i), [i])) for i in ids}


def trend(db: DB, chain: list[int], where, runs: list[dict] | None = None) -> list[dict] | None:
    """The chain's metric series [{name, unit, points: [{run, task, at, value, delta}]}], or None."""
    if runs is None:
        marks = ",".join("?" * len(chain))
        runs = db.q(f"SELECT id,task,dir,status,started,ended FROM runs WHERE task IN ({marks}) ORDER BY id", chain)
    series: dict[str, dict] = {}
    seen_tasks = set()
    for r in runs:
        got = _run_metrics(r)
        seen_tasks.update([r["task"]] if got else [])
        for name, value, unit in got:
            s = series.setdefault(name, {"name": name, "unit": unit, "points": []})
            s["points"].append({"run": r["id"], "task": r["task"], "ts": r["ended"] or r["started"], "value": value,
                                "unit": unit})
    # A task whose run folders are gone still has its last hand-off: one point at its last update.
    marks = ",".join("?" * len(chain))
    for t in db.q(f"SELECT id,updated,result FROM tasks WHERE id IN ({marks}) AND result LIKE '%\"metrics\"%'", chain):
        if t["id"] in seen_tasks:
            continue
        last = max((r["id"] for r in runs if r["task"] == t["id"]), default=None)
        for name, value, unit in metrics_of(load_result(t["result"])):
            s = series.setdefault(name, {"name": name, "unit": unit, "points": []})
            s["points"].append({"run": last, "task": t["id"], "ts": t["updated"], "value": value, "unit": unit})
    out = []
    for s in series.values():
        units = [x["unit"] for x in s["points"] if x["unit"]]
        s["unit"] = max(units, key=units.count) if units else ""   # the most common unit; a bare number fits any
        pts = [x for x in s["points"] if x.pop("unit") in ("", s["unit"])]
        pts = sorted(pts, key=lambda x: (x["ts"] or 0, x["run"] or 0))[-MAX_POINTS:]
        if len(pts) < 2:
            continue
        prev = None
        for x in pts:
            ts = x.pop("ts")
            x["at"] = timefmt.long(ts, where) if ts else ""
            x["delta"] = None if prev is None else x["value"] - prev
            prev = x["value"]
        out.append({**s, "points": pts})
    out.sort(key=lambda s: (-len(s["points"]), s["name"]))
    return out[:MAX_SERIES] or None


def for_tasks(db: DB, ids, where, now: float | None = None) -> dict[int, list[dict]]:
    """{task id: its chain's trend} for those of `ids` whose chain has one."""
    by_task = chains(db, ids, now)
    every = sorted({i for c in by_task.values() for i in c})
    runs: dict[int, list[dict]] = {}
    for k in range(0, len(every), 500):
        part = every[k:k + 500]
        for r in db.q(f"SELECT id,task,dir,status,started,ended FROM runs WHERE task IN ({','.join('?' * len(part))}) "
                      "ORDER BY id", part):
            runs.setdefault(r["task"], []).append(r)
    done: dict[tuple, list | None] = {}
    out = {}
    for i, chain in by_task.items():
        key = tuple(chain)
        if key not in done:
            done[key] = trend(db, chain, where, [r for t in chain for r in runs.get(t, [])])
        if done[key]:
            out[i] = done[key]
    return out
