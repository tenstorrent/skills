"""How many reference trials run at once while a model's server refuses calls for capacity.

A trial row's `server` (serverhealth.judge) counts the model calls it made (`calls`) and those the server
refused for capacity (`failed_by.capacity`). Over the most recent rows holding at least WINDOW_CALLS calls
(or the last WINDOW_S seconds, whichever is more), a refusal rate over OVER (20%) lowers the level: to the
model's probed safe level (`<model-bench>/concurrency/<model, / as ->.json`, its `safe_level`: the most
trials at once with under 5% refusals, from a short probe), else one step down STEPS (6, 4, 3, 2, 1). Once
lowered, it keeps stepping down while the rate stays at SAFE (5%) or more. It never goes up within a run.
Trials already running are never stopped: the runner only starts no new trial while more than the level run.
Each change is logged to stderr and appended to `<slug>.backoff.jsonl` beside the rows."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from threading import Condition

OVER, SAFE = 0.20, 0.05
WINDOW_CALLS, WINDOW_S, MIN_CALLS = 100, 600, 20
STEPS = (6, 4, 3, 2, 1)


def probe_file(model: str) -> Path:
    from . import modelbench
    return modelbench.home() / "concurrency" / f"{model.replace('/', '-')}.json"


def safe_level(model: str) -> int | None:
    """The probed level with under 5% refusals, or None with no probe record."""
    try:
        v = json.loads(probe_file(model).read_text()).get("safe_level")
    except (OSError, ValueError, AttributeError):
        return None
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else None


def _counts(row: dict) -> tuple[int, int]:
    sv = row.get("server") if isinstance(row.get("server"), dict) else {}
    by = sv.get("failed_by") if isinstance(sv.get("failed_by"), dict) else {}
    return int(sv.get("calls") or 0), int(by.get("capacity") or 0)


class Backoff:
    """The level trials run at; `slot()` waits for a free one, `seen(row)` feeds each finished row."""

    def __init__(self, model: str, level: int, log: Path | None = None, clock=time.time, probed: int | None = None):
        self.model, self.level, self.log, self.clock = model, max(1, int(level)), log, clock
        self.probed = safe_level(model) if probed is None else probed
        self.rows: list[tuple[float, int, int]] = []
        self.backing, self.running = False, 0
        self.cv = Condition()

    def rate(self) -> tuple[float, int] | None:
        """Capacity refusals over the recent window, and its calls; None while it holds too few calls."""
        now, calls, refused = self.clock(), 0, 0
        for at, c, r in reversed(self.rows):
            if calls >= WINDOW_CALLS and now - at > WINDOW_S:
                break
            calls, refused = calls + c, refused + r
        return (refused / calls, calls) if calls >= MIN_CALLS else None

    def seen(self, row: dict) -> int:
        c, r = _counts(row)
        with self.cv:
            if c:
                self.rows.append((self.clock(), c, r))
            got = self.rate()
            if got is None or self.level <= 1:
                return self.level
            pct, calls = got
            if pct > OVER or (self.backing and pct >= SAFE):
                lower = [n for n in STEPS if n < self.level]
                new = (self.probed if self.probed and self.probed < self.level and pct > OVER
                       else lower[0] if lower else 1)
                self._set(new, pct, calls)
            return self.level

    def _set(self, new: int, pct: float, calls: int) -> None:
        old, self.level, self.backing, self.rows = self.level, new, True, []   # the next window is the new level's
        why = "probed safe level" if new == self.probed else "no probe record: one step down" \
            if self.probed is None else "still refused: one step down"
        rec = {"at": self.clock(), "model": self.model, "from": old, "to": new, "refusal_pct": round(pct * 100, 1),
               "calls": calls, "why": why}
        print(f"bench back-off: {self.model} capacity refusals {rec['refusal_pct']}% over {calls} calls; "
              f"trials at once {old} -> {new} ({why})", file=sys.stderr, flush=True)
        if self.log is not None:
            from .project import durable_append
            try:
                durable_append(self.log, json.dumps(rec) + "\n")
            except OSError:
                pass
        self.cv.notify_all()

    def slot(self) -> None:
        with self.cv:
            self.cv.wait_for(lambda: self.running < self.level)
            self.running += 1

    def free(self) -> None:
        with self.cv:
            self.running -= 1
            self.cv.notify_all()
