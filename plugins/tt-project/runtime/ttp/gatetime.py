# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Each project's landing check time: how long its push checks (delivery.push_checks) take before a change lands.

A landing waits for the project's push checks, so they are a smoke suite: under 10 minutes on their host,
aim 5. Slower tests belong in delivery.post_land_checks, which run after landing and never hold the queue.

- Record: each passing push check run's wall time is one row of LOG under the project's state (record(),
  from push.record_check_s), the newest KEEP rows kept.
- Measure (stats()): median and p90 over the last 24 h and over the last LAST_N runs. `ttp status` and the
  daily review show it (status_line()).
- Flag (flag(), each daemon pass, model-free): once the median of the last LAST_N runs is over
  `checks.smoke_max_s` (seconds; default DEFAULT_MAX_S, 0: off), one `gate_slow` observation goes to the
  project's own coordinator per day. It names the slowest tests the newest push log shows (pytest's
  --durations lines) and suggests a smoke split; the project changes its own push checks.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
from pathlib import Path

from .project import durable_append, durable_write

LOG = "push-check-times.jsonl"
KEEP = 200
DAY_S = 86400.0
LAST_N = 10
DEFAULT_MAX_S = 600.0
AIM_S = 300.0
EVENT = "gate_slow"
SLOWEST = 10
# A pytest --durations line: "12.34s call     tests/x.py::test_y"
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s+(?:call|setup|teardown)\s+(\S+::\S+)", re.M)


def record(state: Path | None, seconds: float, now: float | None = None) -> None:
    """Keep one passing push check run's wall time; a failed write only loses a sample."""
    if state is None or not seconds or seconds <= 0:
        return
    path = Path(state) / LOG
    try:
        if path.exists() and path.stat().st_size > KEEP * 64:
            lines = path.read_text().splitlines()
            if len(lines) > KEEP:
                durable_write(path, "".join(f"{x}\n" for x in lines[-KEEP:]))
        durable_append(path, json.dumps({"ts": time.time() if now is None else now, "s": round(seconds, 1)}) + "\n")
    except OSError:
        pass


def _rows(state: Path | None) -> list[tuple[float, float]]:
    try:
        lines = (Path(state) / LOG).read_text().splitlines() if state is not None else []
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
            ts, s = float(r["ts"]), float(r["s"])
        except (ValueError, KeyError, TypeError):
            continue
        if s > 0:
            out.append((ts, s))
    return out


def _summary(xs: list[float]) -> dict | None:
    if not xs:
        return None
    xs = sorted(xs)
    return {"n": len(xs), "median_s": round(statistics.median(xs), 1),
            "p90_s": round(xs[max(0, math.ceil(0.9 * len(xs)) - 1)], 1)}   # nearest rank


def stats(state: Path | None, now: float | None = None) -> dict:
    """{"day": the last 24 h's summary, "last": the last LAST_N runs' summary} (each None with no runs)."""
    now = time.time() if now is None else now
    rows = _rows(state)
    return {"day": _summary([s for ts, s in rows if now - DAY_S <= ts <= now]),
            "last": _summary([s for _, s in rows[-LAST_N:]])}


def max_s(cfg: dict | None) -> float:
    """checks.smoke_max_s in seconds (DEFAULT_MAX_S when unset or malformed; 0: no flag)."""
    v = ((cfg or {}).get("checks") or {}).get("smoke_max_s")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
        return float(v)
    return DEFAULT_MAX_S


def _m(s: float) -> str:
    return f"{s / 60:.1f} min"


def status_line(state: Path | None, cfg: dict | None, now: float | None = None) -> str:
    """One line for `ttp status`: the landing check time, and whether it is over the smoke limit; '' with no runs."""
    st = stats(state, now)
    if not st["last"]:
        return ""
    parts = [f"{label} median {_m(v['median_s'])}, p90 {_m(v['p90_s'])} ({v['n']} runs)"
             for label, v in (("24 h", st["day"]), ("last runs", st["last"])) if v]
    lim = max_s(cfg)
    over = f"; over the {_m(lim)} smoke limit (checks.smoke_max_s): move slow tests to delivery.post_land_checks" \
        if lim and st["last"]["median_s"] > lim else ""
    return "landing checks: " + "; ".join(parts) + over


def slowest(log_dir: Path | None, n: int = SLOWEST) -> list[tuple[float, str]]:
    """The `n` slowest tests (seconds, node id) the newest push log with pytest --durations lines shows."""
    try:
        logs = sorted(Path(log_dir).glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:20]
    except (OSError, TypeError):
        return []
    for f in logs:
        try:
            found = _DURATION.findall(f.read_text(errors="replace"))
        except OSError:
            continue
        if found:
            best: dict[str, float] = {}
            for s, node in found:
                best[node] = max(best.get(node, 0.0), float(s))
            return sorted(((s, k) for k, s in best.items()), reverse=True)[:n]
    return []


def flag(db, state: Path | None, cfg: dict | None, log_dir: Path | None = None, now: float | None = None) -> str:
    """Raise one `gate_slow` observation a day while the last runs' median is over checks.smoke_max_s;
    returns its text ('' when none was raised)."""
    now = time.time() if now is None else now
    lim = max_s(cfg)
    last = stats(state, now)["last"]
    if not lim or not last or last["median_s"] <= lim:
        return ""
    fp = f"{EVENT}:{time.strftime('%Y-%m-%d', time.gmtime(now))}"
    if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return ""
    tests = slowest(log_dir)
    named = ("Slowest tests in the newest push log: " + ", ".join(f"{k} {s:.0f}s" for s, k in tests) + ". ") if tests \
        else "Add --durations=30 to the pytest push check to see the slowest tests. "
    text = (f"Landing checks are slow: median {_m(last['median_s'])} over the last {last['n']} push check runs, "
            f"over the {_m(lim)} smoke limit (checks.smoke_max_s; aim {_m(AIM_S)}). " + named
            + "Split them: keep a smoke subset in delivery.push_checks and move the slower tests to "
            "delivery.post_land_checks, which run after landing.")
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
         (now, "daemon", "observation", fp, "normal", text[:4000], "queued"))
    return text
