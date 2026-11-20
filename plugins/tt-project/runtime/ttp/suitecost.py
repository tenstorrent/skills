# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Required-suite cost watch, model-free.

When a push batch's checks pass, `record` reads the `ttp-suite: wall Xs cpu Ys workers N` line and
pytest's `N passed` line from the check output and stores (batch, sha, version, wall, cpu, tests)
in the project's kv store (SAMPLES_KEY, the last KEEP_S). When CPU per test is over HIGH times the
median of the last WINDOW_S of earlier batches (at least MIN_SAMPLES), one low event is raised,
naming the batch, its version, the first batch over the threshold and the numbers before and after.
One event per episode: it re-arms once CPU per test is under LOW times the median. A check that
prints no suite line (or no test count) records nothing, so such projects stay silent."""
from __future__ import annotations

import json
import re
import statistics
import time

from .db import DB

SAMPLES_KEY, STATE_KEY = "suite_cost_samples", "suite_cost_state"
EVENT = "suite_cost_regression"
HIGH, LOW, MIN_SAMPLES = 1.5, 1.2, 5
WINDOW_S, KEEP_S = 7 * 86400, 14 * 86400
SUITE = re.compile(r"^ttp-suite: wall ([\d.]+)s cpu ([\d.]+)s(?: workers (\d+))?", re.M)
PASSED = re.compile(r"\b(\d+) passed\b")


def parse(text: str) -> dict | None:
    """{wall, cpu, tests} from check output; None when either line is missing."""
    s, ts = list(SUITE.finditer(text or "")), list(PASSED.finditer(text or ""))
    if not s or not ts or int(ts[-1].group(1)) <= 0:
        return None
    return {"wall": float(s[-1].group(1)), "cpu": float(s[-1].group(2)), "tests": int(ts[-1].group(1))}


def per_test(s: dict) -> float:
    return float(s["cpu"]) / max(int(s["tests"]), 1)


def median(db: DB, now: float | None = None, before: float | None = None) -> float | None:
    """Median CPU per test over the last WINDOW_S (samples before `before`); None under MIN_SAMPLES."""
    now = time.time() if now is None else now
    rows = [s for s in db.kv(SAMPLES_KEY, []) or []
            if s["ts"] >= now - WINDOW_S and (before is None or s["ts"] < before)]
    return statistics.median(per_test(s) for s in rows) if len(rows) >= MIN_SAMPLES else None


def record(db: DB, batch: str, sha: str, version: str | None, output: str, now: float | None = None) -> str:
    """Store a passing batch's suite cost and judge it; the event text raised, '' when none."""
    got = parse(output)
    if not got:
        return ""
    now = time.time() if now is None else now
    med = median(db, now)
    sample = {"batch": str(batch), "sha": sha, "version": version or "", "ts": now, **got}
    db.set_kv(SAMPLES_KEY, [s for s in db.kv(SAMPLES_KEY, []) or [] if s["ts"] >= now - KEEP_S] + [sample])
    state = db.kv(STATE_KEY, {}) or {}
    if med is None:
        return ""
    cost = per_test(sample)
    if state.get("open"):
        if cost < LOW * med:
            db.set_kv(STATE_KEY, {})
        return ""
    if cost <= HIGH * med:
        return ""
    first = sample   # the start of the run of batches over the threshold that ends here
    for s in reversed(db.kv(SAMPLES_KEY, [])):
        if per_test(s) <= HIGH * med:
            break
        first = s
    text = (f"required-suite CPU per test regressed: batch {batch}" + (f" (version {version})" if version else "")
            + f" {cost * 1000:.0f} ms/test (cpu {got['cpu']:.0f}s, {got['tests']} tests) vs 7-day median {med * 1000:.0f} ms/test, "
            f"{cost / med:.1f}x; first batch over {HIGH}x: {first['batch']}"
            + (f" (version {first['version']})" if first["version"] else "")
            + f" at {per_test(first) * 1000:.0f} ms/test")
    db.set_kv(STATE_KEY, {"open": True, "first": first["batch"], "ts": now})
    db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
         (now, "push", EVENT, "low", text, json.dumps({"batch": batch, "first": first["batch"], "median": med, "cost": cost}), "new"))
    return text


def audit_line(db: DB, now: float | None = None) -> str:
    """The latest suite wall/CPU and the 7-day median CPU per test; '' when no batch recorded one."""
    now = time.time() if now is None else now
    rows = db.kv(SAMPLES_KEY, []) or []
    if not rows:
        return ""
    last, med = rows[-1], median(db, now)
    return (f"required suite: latest wall {last['wall']:.0f}s cpu {last['cpu']:.0f}s ({last['tests']} tests, batch {last['batch']}); 7-day median "
            + (f"{med * 1000:.0f} ms cpu/test" if med is not None else f"n/a (under {MIN_SAMPLES} batches)"))
