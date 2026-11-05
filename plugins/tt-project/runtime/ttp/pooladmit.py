# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Admission of a sharded run on a test pool that granted fewer slots than it planned (`ttp pool-admit`).

A check script that runs its own test pool plans N shards and takes the slots it is granted. Granted
fewer (K < N), each slot runs about N/K planned shards one after another, and a run that fit the check's
time limit on N slots may be certain to run past it on K. Before it starts the shards on K slots the
script asks `ttp pool-admit --planned N --granted K [--files F] [--what <pool>]`:

- exit 0, admitted: K >= N (nothing changes), no time limit is known here (outside a push check or a
  checks run with a wall cap), or the estimate fits the time the check has left;
- exit SHORT (3), refused: the estimate on K slots is over the time left. The script never starts the run
  on those K slots. stdout holds one number, the fewest slots that would fit (none once no time is left);
  stderr says why. The script then gives the slots back and waits for that many inside `ttp pool-wait`
  (bounded by delivery.push_pool_wait_s; in a push check that wait counts against the limit, so it asks
  again once granted), or runs the set on this host's own checks slots (`ttp checks-shard`), or both:
  pool slots plus this host's.

The estimate is the check's own history on this machine (estimate()): the median seconds per file,
when F is given and earlier runs gave theirs, else per planned shard, each in slot-seconds, so
work = per-file x F (or per-shard x N), spread over K slots. With no history it is conservative: each
planned shard is taken to need CONSERVATIVE_SHARE of the check's whole limit, so a cut to fewer than half
the planned slots is refused until a run on enough slots was measured. `ttp pool-admit --ran <s>` with
the run's --granted, --planned and --files records a passing run's time (its pool waits left out)
into that history (HISTORY, the newest KEEP per pool).

The time left: a push check's limit counts wall time less only its waits for a grant, its test-pool waits
included (push._timed_wait), so the push gives each check LIMIT_ENV (its start and limit) and the left time
takes off the same grant waits (checkwait.grants); a `ttp checks` run gives WALL_ENV, its start and wall
cap, waits included. With both, the tighter one counts.

The limit itself is never scaled: refusing is enough to start no run that is certain to time out, and a
scaled limit would let one cut run hold a push for longer than the project allowed."""
from __future__ import annotations

import json
import math
import os
import statistics
import time
from pathlib import Path

from . import checkwait, project

SHORT = 3
LIMIT_ENV = "TTP_CHECK_LIMIT"          # "<start epoch>:<limit s>": a push check's run-time limit
WALL_ENV = "TTP_CHECKS_WALL"      # "<start epoch>:<cap s>": a `ttp checks` run's wall-time cap
CONSERVATIVE_SHARE = 0.5   # no history: a planned shard is taken to need this share of the whole limit
HISTORY = "pool-runs.json"
KEEP = 20


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def time_left(now: float | None = None, env: dict | None = None) -> tuple[float, float] | None:
    """(seconds the check has left, its whole limit), or None when no limit is known here."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    out: list[tuple[float, float]] = []
    for name in (LIMIT_ENV, WALL_ENV):
        start, _, limit = str(env.get(name) or "").partition(":")
        start, limit = _num(start), _num(limit)
        if start is None or limit is None or limit <= 0:
            continue
        waited = 0.0
        if name == LIMIT_ENV and env.get(checkwait.ENV):
            waited = checkwait.grants(env[checkwait.ENV], start, now)[0]
        out.append((limit - (now - start - waited), limit))
    return min(out) if out else None


def _path() -> Path:
    return project.HOME_DIR / "check-slots" / HISTORY


def _key(what: str) -> str:
    name = Path(os.environ.get("TTP_PROJECT") or "").name
    return f"{name}/{what or 'pool'}"


def _load() -> dict:
    try:
        v = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def record(what: str, seconds: float, granted: int, planned: int | None = None, files: int | None = None) -> None:
    """Add a passing run's time (seconds on `granted` slots) to the pool's history, as slot-seconds per
    planned shard and per file. A failed write only costs the history."""
    if seconds <= 0 or granted <= 0:
        return
    rec = {"at": round(time.time()), "per_shard": round(seconds * granted / planned, 2) if planned else None,
           "per_file": round(seconds * granted / files, 3) if files else None}
    data = _load()
    key = _key(what)
    data[key] = ([r for r in data.get(key) or [] if isinstance(r, dict)] + [rec])[-KEEP:]
    try:
        project.durable_write(_path(), json.dumps(data))
    except OSError:
        pass


def _median(what: str, field: str) -> float | None:
    vals = [v for r in _load().get(_key(what)) or [] if isinstance(r, dict)
            and (v := _num(r.get(field))) is not None and v > 0]
    return statistics.median(vals) if vals else None


def estimate(what: str, planned: int, granted: int, limit: float, files: int | None = None) -> tuple[float, str]:
    """(estimated seconds of the run on `granted` slots, how it was estimated)."""
    if files and (per := _median(what, "per_file")) is not None:
        return per * files / granted, f"median {per:.1f} slot-s per file x {files} files"
    if (per := _median(what, "per_shard")) is not None:
        return per * planned / granted, f"median {per:.0f} slot-s per planned shard x {planned}"
    per = CONSERVATIVE_SHARE * limit
    return per * planned / granted, (f"no timing history: each planned shard taken as {CONSERVATIVE_SHARE:g} "
                                     f"of the {limit:.0f} s limit")


def admit(planned: int, granted: int, files: int | None = None, what: str = "",
          now: float | None = None) -> tuple[bool, int | None, str]:
    """(start the run on `granted` slots?, the fewest slots that fit when not, why)."""
    if granted >= planned:
        return True, None, ""
    if (left := time_left(now)) is None:
        return True, None, "no time limit known here"
    left_s, limit = left
    est, how = estimate(what, planned, granted, limit, files)
    if est <= left_s:
        return True, None, f"{granted} of {planned} slots: about {est:.0f} s ({how}), {left_s:.0f} s left"
    need = math.ceil(est * granted / left_s - 1e-9) if left_s > 0 else None
    why = (f"{granted} of {planned} planned slots would take about {est:.0f} s ({how}), over the "
           f"{max(left_s, 0):.0f} s this check has left: not started on them; "
           + (f"{need} slots would fit" if need else "no time is left"))
    return False, need, why
