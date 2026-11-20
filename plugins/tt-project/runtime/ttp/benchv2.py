"""Repeat runs and grades with statistics for the code benches (bench v2).

v1's verdicts were noise: one run per arm, one grade per diff, a table that a single 1-point move
flips. v2 measures the noise itself. A subset of tasks runs every arm N times
(`bench.repeats.runs`) and each diff is graded M times (`bench.repeats.grades`), so the table can
report what a verdict is worth: how often the landed verdict flips from run to run and from grade
to grade, pass rates with 95% intervals, and paired differences between arms on the same tasks. A
claim "A beats B" needs the interval of the paired difference to exclude zero; anything else is
printed as "cannot separate" (`separates`).

The subset is drawn by `pick_repeat` before anything runs, seeded and recorded in the run's
record, so every arm and every grade covers the same tasks. Everything here is model-free: rows in,
numbers out.
"""
from __future__ import annotations

import random
from pathlib import Path

# -- intervals


def wilson(passed: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson 95% interval of a pass rate: an empty one is (0, 1)."""
    if n <= 0:
        return 0.0, 1.0
    p = passed / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * (p * (1 - p) + z * z / (4 * n)) ** 0.5 / (n ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


# -- rates


def pass_rate(rows: list[dict], field: str = "landed") -> dict:
    """{n, passed, rate, low, high}: the share of rows whose `field` is true with its Wilson 95%."""
    rows = [r for r in rows if r.get(field) is not None]
    n = len(rows)
    passed = sum(1 for r in rows if r[field])
    low, high = wilson(passed, n)
    return {"n": n, "passed": passed, "rate": round(passed / n, 4) if n else None,
            "low": round(low, 4), "high": round(high, 4)}


def paired(rows_a: list[dict], rows_b: list[dict], field: str = "landed") -> dict | None:
    """The paired difference between two arms on the tasks they share: each side's passed share on
    those tasks, the difference, and its 95% interval (Newcombe's paired interval). None when they
    share no task. `rows_a`/`rows_b`: [{task, landed}]"""
    a = {str(r["task"]): r for r in rows_a if r.get(field) is not None}
    b = {str(r["task"]): r for r in rows_b if r.get(field) is not None}
    shared = sorted(set(a) & set(b))
    if not shared:
        return None
    na = len(shared)
    pa = sum(1 for t in shared if a[t][field])
    pb = sum(1 for t in shared if b[t][field])
    diff = (pa - pb) / na
    la, ha = wilson(pa, na)
    lb, hb = wilson(pb, na)
    lo = diff - ((pa / na - la) ** 2 + (hb - pb / na) ** 2) ** 0.5
    hi = diff + ((pa / na - la) ** 2 + (hb - pb / na) ** 2) ** 0.5
    return {"n": na, "a_passed": pa, "b_passed": pb, "a_rate": round(pa / na, 4),
            "b_rate": round(pb / na, 4), "diff": round(diff, 4),
            "low": round(lo, 4), "high": round(hi, 4)}


def separates(d: dict | None) -> bool:
    """A difference separates two arms only when its interval excludes zero; otherwise the honest
    verdict is "cannot separate", whatever the point estimate says."""
    return bool(d) and (d["low"] > 0 or d["high"] < 0)


def verdict(d: dict | None, a: str, b: str) -> str:
    """The one-line verdict for a pair: which arm wins, or that they cannot be separated."""
    if not d:
        return f"cannot compare {a} and {b} (no shared task)"
    lo, hi = d["low"], d["high"]
    if lo > 0:
        return f"{a} beats {b} (n={d['n']}, interval {lo:+.4f}..{hi:+.4f})"
    if hi < 0:
        return f"{b} beats {a} (n={d['n']}, interval {lo:+.4f}..{hi:+.4f})"
    return f"cannot separate {a} and {b} (n={d['n']}, interval {lo:+.4f}..{hi:+.4f})"
