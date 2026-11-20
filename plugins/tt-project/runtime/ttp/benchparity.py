# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Parity of bench table rows: which rows ran under the same conditions, model-free.

Every bench row a table shows records how its arm was called and what it ran under: the harness and
agent, provider, model, effort, the kind of route (a bridge to a logged-in CLI or an endpoint; never a
key or URL), the task set (a hash of the sorted task names and their count), the time limit and what it
counts, and the rerun rule. compare() takes the value most rows share for each condition (FIELDS; the
first row's on a tie) and lists every row that differs, or does not record it, as not comparable; a
table writer prints section(): the differences, then "How each arm was called" (one line per row).
Provider, model, effort and route differ between arms by design and are shown, never compared.
"""
from __future__ import annotations

import hashlib
from collections import Counter

FIELDS = ("harness", "agent", "task_set", "tasks", "limit_s", "limit_counts", "rerun_rule")
CALLED = ("provider", "kind", "model", "effort", "harness", "agent")
MISSING = "(not recorded)"


def task_set(names) -> str:
    """A short, order-free hash of a task list."""
    return hashlib.sha256("\n".join(sorted(str(n) for n in names)).encode()).hexdigest()[:12]


def label(row: dict) -> str:
    return str(row.get("arm") or f"{row.get('model', '?')} ({row.get('effort') or 'default'})")


def _val(row: dict, f: str) -> str:
    v = row.get(f)
    return MISSING if v is None or v == "" else str(v)


def compare(rows: list[dict], fields=FIELDS) -> dict:
    """{"differs": {field: {"expected": v, "rows": {label: value}}}, "not_comparable": [labels]}."""
    differs: dict = {}
    bad: list[str] = []
    for f in fields:
        vals = [_val(r, f) for r in rows]
        if len(set(vals)) < 2:
            continue
        counts = Counter(vals)
        top = max(counts.values())
        want = next(v for v in vals if counts[v] == top)
        odd = {label(r): v for r, v in zip(rows, vals) if v != want}
        differs[f] = {"expected": want, "rows": odd}
        bad += [x for x in odd if x not in bad]
    return {"differs": differs, "not_comparable": [label(r) for r in rows if label(r) in bad]}


def how_called(rows: list[dict], extra=()) -> list[str]:
    """One line per row: how its arm was called (CALLED, then `extra` fields it records)."""
    out = []
    for r in rows:
        parts = [f"{f} {r[f]}" for f in (*CALLED, *extra) if r.get(f) not in (None, "")
                 and not (f == "model" and str(r[f]) in label(r))]
        out.append(f"- {label(r)}: " + "; ".join(parts))
    return out


def section(rows: list[dict], fields=FIELDS, extra=()) -> list[str]:
    """The markdown a table writer appends: parity, then how each arm was called."""
    got = compare(rows, fields)
    out = ["## Parity", ""]
    if not got["differs"]:
        out.append(f"All {len(rows)} rows ran under the same conditions ({', '.join(fields)}).")
    else:
        for f, d in got["differs"].items():
            out.append(f"- {f} differs (most rows: {d['expected']}): "
                       + ", ".join(f"{k} {v}" for k, v in d["rows"].items()))
        out.append(f"- Not comparable: {', '.join(got['not_comparable'])}")
    return out + ["", "## How each arm was called", ""] + how_called(rows, extra) + [""]
