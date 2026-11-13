#!/usr/bin/env python3
"""Explained hours of an eval run: the run replayed on stage 11's serving rates (agentic-serving, Phases).

    explain_hours.py --rates doc/benchmark/serving_rates.json --calls <calls.json> [--agents N] [--out file]

``calls.json`` comes from the run's per-call accounting::

    {"agents": 10,
     "tasks": [{"task": "django__django-11299", "start": <unix s>, "end": <unix s>,     # optional
                "outside_s": 812.0,                                                 # optional
                "calls": [{"start": <unix s>, "end": <unix s>, "uncached_input": 1830,
                           "output": 412, "context": 23100}]}]}

``context`` is the call's final context (prompt plus output). ``active`` is the number of requests in
flight at the call's midpoint (calls of all tasks whose span contains it). With ``server_batch`` (the
max-num-seqs the run served at, from calls.json or ``--server-batch``) and rows measured at that batch
at more than one request concurrency (rows may carry ``concurrency``; default the batch), rates are
interpolated linearly in the active count between those concurrencies: a B32 server with three
requests in flight is neither the B32 row (32 users) nor the B8 row (another server). Otherwise each call
is priced at the smallest validated batch that holds the active requests, else the largest, and a run
that names its server batch without such rows is marked approximate. Uncached input is priced at the prefill rate for its prompt
length plus output at the decode rate for its mean context (prompt plus half the output). Rates
between measured input lengths are interpolated linearly in seconds per token; beyond the longest row
they are extrapolated from the last two rows (never faster than the last row) and the result is marked
approximate, as is a batch with no rows (the nearest batch with rows is used). A task's time outside the
server is ``outside_s``, else its span minus its calls' measured spans, else 0. Tasks start on the
agents in their observed order; the explained wall is when the last one ends.

The output is JSON: ``explained_hours``, ``approximate`` with ``reasons``, per task hours, the longest
context priced and the longest measured row. It never fails on a missing row: a number, labelled, is
more useful than a null that blocks the report.
"""
from __future__ import annotations

import argparse
import bisect
import heapq
import json
import sys
from pathlib import Path


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


class Rates:
    """Seconds per token by batch and input length, from serving_rates.json rows (full-concurrency rows by
    batch; rows below full concurrency by (batch, concurrency))."""

    def __init__(self, rates: dict):
        self.rows: dict = {}
        for row in rates.get("rows") or []:
            if not isinstance(row, dict):
                continue
            batch, length = row.get("batch"), row.get("input_tokens")
            if not (isinstance(batch, int) and isinstance(length, int) and number(row.get("prefill_tok_s"))
                    and number(row.get("decode_tok_s_user"))):
                continue
            concurrency = row.get("concurrency") if isinstance(row.get("concurrency"), int) else batch
            key = batch if concurrency >= batch else (batch, concurrency)
            self.rows.setdefault(key, []).append((length, 1 / row["prefill_tok_s"], 1 / row["decode_tok_s_user"]))
        for key in self.rows:
            self.rows[key].sort()
        if not any(isinstance(key, int) for key in self.rows):
            raise SystemExit("serving_rates.json has no full-concurrency row with prefill_tok_s and decode_tok_s_user")
        validated = [b for b in rates.get("validated_batches") or [] if isinstance(b, int) and b > 0]
        full = [key for key in self.rows if isinstance(key, int)]
        self.batches = sorted(set(validated) or set(full))
        self.longest = max(rows[-1][0] for rows in self.rows.values())

    def levels(self, batch: int) -> list[tuple[int, object]]:
        """(concurrency, row key) measured at this batch, lowest concurrency first."""
        found = [(key[1], key) for key in self.rows if isinstance(key, tuple) and key[0] == batch]
        if batch in self.rows:
            found.append((batch, batch))
        return sorted(found)

    def on_server(self, batch: int, active: int, context: float, column: int, reasons: set) -> float:
        """Seconds per token on a server of this batch with `active` requests in flight."""
        levels = self.levels(batch)
        if len(levels) < 2:
            reasons.add(f"no rows below full concurrency at server batch {batch}; few requests in flight are "
                        "priced as a full batch")
            key = levels[0][1] if levels else self.batch_for(active, reasons)
            return self.per_token(key, context, column, reasons)
        active = min(max(active, levels[0][0]), levels[-1][0])
        for (c0, k0), (c1, k1) in zip(levels, levels[1:]):
            if c0 <= active <= c1:
                y0 = self.per_token(k0, context, column, reasons)
                y1 = self.per_token(k1, context, column, reasons)
                return y0 + (y1 - y0) * (active - c0) / (c1 - c0) if c1 > c0 else y1
        return self.per_token(levels[-1][1], context, column, reasons)

    def batch_for(self, active: int, reasons: set) -> int:
        full = [key for key in self.rows if isinstance(key, int)]
        batch = next((b for b in self.batches if b >= active), self.batches[-1] if self.batches else max(full))
        if batch in self.rows:
            return batch
        near = min(full, key=lambda b: (abs(b - batch), -b))
        reasons.add(f"batch {batch} has no rate rows; priced at batch {near}")
        return near

    def per_token(self, key, context: float, column: int, reasons: set) -> float:
        rows = self.rows[key]
        batch = key if isinstance(key, int) else f"{key[0]} at concurrency {key[1]}"
        lengths = [row[0] for row in rows]
        if context <= lengths[0]:
            return rows[0][column]
        if context <= lengths[-1]:
            i = bisect.bisect_left(lengths, context)
            (x0, *a), (x1, *b) = rows[i - 1], rows[i]
            y0, y1 = a[column - 1], b[column - 1]
            return y0 + (y1 - y0) * (context - x0) / (x1 - x0)
        reasons.add(f"contexts above the longest measured row ({lengths[-1]} at batch {batch}) are extrapolated")
        if len(rows) == 1:
            return rows[-1][column]
        (x0, *a), (x1, *b) = rows[-2], rows[-1]
        y0, y1 = a[column - 1], b[column - 1]
        slope = max(0.0, (y1 - y0) / (x1 - x0))
        return y1 + slope * (context - x1)


def explain(rates: dict, calls: dict, agents: int | None = None, server_batch: int | None = None) -> dict:
    table = Rates(rates)
    server_batch = server_batch or calls.get("server_batch")
    reasons: set[str] = set()
    tasks = [t for t in calls.get("tasks") or [] if isinstance(t, dict)]
    agents = agents or calls.get("agents")
    if not (isinstance(agents, int) and agents > 0):
        raise SystemExit("give the run's agent count (--agents or calls.json agents)")
    spans = sorted((c["start"], c["end"]) for t in tasks for c in t.get("calls") or []
                   if isinstance(c, dict) and number(c.get("end")) and isinstance(c.get("start"), (int, float)))
    starts = [s for s, _ in spans]
    longest_context = 0
    per_task = []
    for task in tasks:
        server_s = measured_server_s = 0.0
        for call in task.get("calls") or []:
            if not isinstance(call, dict):
                continue
            output = call.get("output") or 0
            uncached = call.get("uncached_input") or 0
            context = call.get("context") or (uncached + output)
            longest_context = max(longest_context, context)
            if isinstance(call.get("start"), (int, float)) and number(call.get("end")):
                mid = (call["start"] + call["end"]) / 2
                active = sum(1 for s, e in spans[: bisect.bisect_right(starts, mid)] if e >= mid) or 1
                measured_server_s += call["end"] - call["start"]
            else:
                active = 1
                reasons.add("calls without start and end are priced at batch 1 concurrency")
            prompt = max(1, context - output)
            if isinstance(server_batch, int):
                server_s += uncached * table.on_server(server_batch, active, prompt, 1, reasons)
                server_s += output * table.on_server(server_batch, active, prompt + output / 2, 2, reasons)
            else:
                batch = table.batch_for(active, reasons)
                server_s += uncached * table.per_token(batch, prompt, 1, reasons)
                server_s += output * table.per_token(batch, prompt + output / 2, 2, reasons)
        if number(task.get("outside_s")) or task.get("outside_s") == 0:
            outside = float(task["outside_s"])
        elif isinstance(task.get("start"), (int, float)) and number(task.get("end")):
            outside = max(0.0, task["end"] - task["start"] - measured_server_s)
        else:
            outside = 0.0
        per_task.append({"task": task.get("task"), "observed_start": task.get("start", min(
            (c.get("start") for c in task.get("calls") or [] if isinstance(c, dict) and c.get("start") is not None),
            default=0)), "server_s": round(server_s, 1), "outside_s": round(outside, 1)})
    slots = [0.0] * agents
    heapq.heapify(slots)
    wall = 0.0
    for task in sorted(per_task, key=lambda t: t["observed_start"] or 0):
        begin = heapq.heappop(slots)
        end = begin + task["server_s"] + task["outside_s"]
        heapq.heappush(slots, end)
        wall = max(wall, end)
        task["hours"] = round((task["server_s"] + task["outside_s"]) / 3600, 4)
    return {"explained_hours": round(wall / 3600, 4), "approximate": bool(reasons), "reasons": sorted(reasons),
            "agents": agents, "longest_context": longest_context, "longest_row": table.longest,
            "tasks": [{k: t[k] for k in ("task", "hours", "server_s", "outside_s")} for t in per_task]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rates", type=Path, required=True)
    parser.add_argument("--calls", type=Path, required=True)
    parser.add_argument("--agents", type=int)
    parser.add_argument("--server-batch", type=int)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = explain(json.loads(args.rates.read_text()), json.loads(args.calls.read_text()), args.agents,
                     args.server_batch)
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
