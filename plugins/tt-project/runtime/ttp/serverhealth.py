"""Server-wait accounting for the benches: an open model is never failed for a slow or overloaded server.

Every model call through a bench's bridge is recorded: the time spent waiting on the server (the request
sent to the first byte, plus stream gaps longer than GAP_S, plus capacity waits), the output tokens and
tokens per second. The cell limit counts the agent's own time only (wall time minus that wait), with a
wall-clock ceiling so a hung server never holds a cell. A cell that hit the limit, the ceiling or a
capacity refusal while its calls ran slower than the model's normal speed is `server`: not scored, never
a fail, and rerun one at a time while a short probe shows the server at normal speed.

A model's normal speed is the median tokens/s and server wait over its own recorded healthy calls (History).
The rerun queue is derived from the cells' rows (the latest row of a cell with outcome `server`): no state
file that could disagree with them.
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path

from .project import durable_write

FACTOR = 2.0            # slower than normal: tokens/s below normal / FACTOR, or server wait above normal * FACTOR
WINDOW = 500            # healthy calls per model that define its normal speed
MIN_CALLS = 10          # fewer healthy calls than this: no normal speed is known yet
CEILING_FACTOR = 3.0    # the wall-clock ceiling, in cell limits
GAP_S = 5.0             # a stream gap longer than this is waiting on the server, not generating
WAIT_FLOOR_S = 5.0      # a median wait under this is never slow, whatever normal is
MIN_OUT = 16            # fewer output tokens than this: the call's tokens/s says nothing
MAX_KEPT = 3000         # per-call rows kept per cell
RECENT = 5              # the cell's last calls (at least) checked on their own: a server that slowed late
CAPACITY_STATUS = (429, 503, 529)
CAPACITY = ("rate_limit", "capacity", "overloaded", "overload", "unavailable", "not_served", "busy")
OUTCOME = "server"
NOT_SCORED = "not scored (server)"
REFUSAL_MAX = 0.2       # a cell whose calls the server refused for capacity more often than this share: not run (server)
RULE = "agent_time"     # the limit rule a cell ran under (its row's `limit_rule`); older rows ran under wall time


class Call:
    """One model call: opened at begin, `byte()` on each chunk read, `end()` once."""

    def __init__(self, log: "Calls", now: float):
        self.log, self.t0, self.first, self.last = log, now, None, now
        self.stall_s = 0.0
        self.done = False

    def byte(self, now: float | None = None) -> None:
        now = self.log.clock() if now is None else now
        with self.log.lock:
            if self.first is None:
                self.first = now
            elif now - self.last > self.log.gap_s:
                self.stall_s += now - self.last
            self.last = now

    def live_wait(self, now: float) -> float:
        """The wait so far of a call still open."""
        if self.first is None:
            return max(now - self.t0, 0.0)
        return self.stall_s + (now - self.last if now - self.last > self.log.gap_s else 0.0)

    def end(self, out_tokens: int = 0, status: str = "ok", now: float | None = None, gen_s: float = 0.0) -> None:
        """`gen_s`: the generation time of a reply that came whole, when its source reports it (the claude CLI's
        API time); else the call's whole duration counts as generation."""
        now = self.log.clock() if now is None else now
        with self.log.lock:
            if self.done:
                return
            self.done = True
            dur = max(now - self.t0, 0.0)
            out = max(int(out_tokens or 0), 0)
            if status != "ok":   # an answer that failed: none of it was the agent's
                wait, gen, slow_s = dur, 0.0, 0.0
            elif self.first is None:   # a reply that came whole: its generation time cannot be split from the wait
                wait, gen, slow_s = 0.0, (min(gen_s, dur) if gen_s > 0 else dur), 0.0
            else:
                wait = (self.first - self.t0) + self.stall_s + (now - self.last if now - self.last > self.log.gap_s else 0.0)
                gen = max(dur - wait, 0.0)
                slow_s = 0.0
            tps = round(out / gen, 2) if out and gen > 0 else 0.0
            norm = self.log.normal_tps
            if status == "ok" and norm and out >= MIN_OUT and tps and tps < norm / self.log.factor:
                # slower than the model's normal speed: the excess over what normal speed would have taken is the server's
                slow_s = max(gen - out / norm, 0.0)
                wait += slow_s
            self.log.add({"t": round(self.t0 - self.log.t0, 1), "dur_s": round(dur, 2), "wait_s": round(wait, 2),
                          "slow_s": round(slow_s, 2), "out": out, "tps": tps, "st": status})
            self.log.open.discard(self)


class Calls:
    """A run's per-call record. Thread-safe; `clock` is injectable for tests."""

    def __init__(self, gap_s: float = GAP_S, clock=time.monotonic):
        self.gap_s, self.clock, self.lock = float(gap_s), clock, threading.Lock()
        self.t0 = clock()
        self.rows: list[dict] = []
        self.open: set[Call] = set()
        self.wait_total, self.dropped = 0.0, 0
        self.extra_wait = 0.0   # waits the bridge slept itself (capacity retries between calls)
        self.normal_tps, self.factor = 0.0, FACTOR   # the model's normal speed (History), when known

    def configure(self, normal_tps: float = 0.0, factor: float = FACTOR) -> None:
        self.normal_tps, self.factor = float(normal_tps or 0.0), float(factor or FACTOR)

    def begin(self) -> Call:
        c = Call(self, self.clock())
        with self.lock:
            self.open.add(c)
        return c

    def add(self, row: dict) -> None:   # under self.lock
        self.wait_total += row["wait_s"]
        if len(self.rows) < MAX_KEPT:
            self.rows.append(row)
        else:
            self.dropped += 1

    def wait_s(self) -> float:
        """The server wait so far, in-flight calls included: what the limit does not count."""
        now = self.clock()
        with self.lock:
            return self.wait_total + self.extra_wait + sum(c.live_wait(now) for c in list(self.open))

    def sleep_wait(self, s: float) -> None:
        """A wait the bridge itself slept between attempts of one call (it is the server's capacity)."""
        with self.lock:
            self.extra_wait += max(float(s), 0.0)

    def summary(self) -> dict:
        with self.lock:
            return summarize(list(self.rows), self.wait_total + self.extra_wait, self.dropped)

    def export(self) -> dict:
        """For the run's exit record: the summary and every call's row."""
        with self.lock:
            return {**summarize(list(self.rows), self.wait_total + self.extra_wait, self.dropped),
                    "per_call": list(self.rows)}


def _med(xs: list[float]) -> float:
    return round(statistics.median(xs), 2) if xs else 0.0


FAILED_KINDS = ("stream", "parse", "capacity", "other")   # a failed call's kind (its row's `st`)


def summarize(rows: list[dict], wait_total: float | None = None, dropped: int = 0) -> dict:
    """One cell's server summary from its call rows: the calls, the wait, tokens, tokens/s, failed calls
    and, when any failed, how many of each kind (`failed_by`, FAILED_KINDS: a stream that broke or ended in
    an error, an answer that did not parse, the server at capacity, other; an older row's "fault" is other)."""
    ok = [r for r in rows if r.get("st") == "ok"]
    by = {k: 0 for k in FAILED_KINDS}
    for r in rows:
        if r.get("st") != "ok":
            by[r.get("st") if r.get("st") in by else "other"] += 1
    tps = [r["tps"] for r in ok if r.get("out", 0) >= MIN_OUT and r.get("tps", 0) > 0]
    wait = sum(r["wait_s"] for r in rows) if wait_total is None else wait_total
    return {"calls": len(rows) + dropped, "failed_calls": len(rows) - len(ok), "wait_s": round(wait, 1),
            "wait_median_s": _med([r["wait_s"] for r in ok]), "wait_max_s": round(max([r["wait_s"] for r in rows], default=0.0), 1),
            "out_tokens": sum(int(r.get("out") or 0) for r in rows), "tps_median": _med(tps),
            "dur_s": round(sum(r["dur_s"] for r in rows), 1), **({"failed_by": by} if len(rows) > len(ok) else {})}


# -- the limit

def agent_s(up_s: float, wait_s: float) -> float:
    """The agent's own time: the run's time minus the server wait (never negative)."""
    return max(float(up_s) - min(max(float(wait_s), 0.0), float(up_s)), 0.0)


def ceiling_s(timeout_s: float, factor: float = CEILING_FACTOR, explicit: float | None = None) -> float:
    return float(explicit) if explicit and float(explicit) > 0 else float(timeout_s) * float(factor)


def limit_hit(up_s: float, wait_s: float, timeout_s: float, ceiling: float, credit_s: float = 0.0) -> str:
    """"" while the run is within its limits, "timeout" once the agent's own time passes the limit (plus the
    lock wait `credit_s`), "ceiling" once the wall clock passes the ceiling (a hung or very slow server)."""
    if ceiling and up_s > ceiling + credit_s:
        return "ceiling"
    if agent_s(up_s, wait_s) > float(timeout_s) + credit_s:
        return "timeout"
    return ""


# -- normal speed

class History:
    """Per model, the (tokens/s, wait) of its recent healthy calls, in a JSON file (atomic writes)."""

    def __init__(self, path: Path | str | None, window: int = WINDOW):
        self.path, self.window = Path(path) if path else None, int(window)

    def _read(self) -> dict:
        try:
            got = json.loads(self.path.read_text()) if self.path else {}
        except (OSError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}

    def add(self, model: str, rows: list[dict]) -> int:
        """Record the healthy calls of `rows` (status ok, enough output): returns how many."""
        if not self.path:
            return 0
        keep = [[r["tps"], r["wait_s"]] for r in rows if r.get("st") == "ok" and r.get("out", 0) >= MIN_OUT and r.get("tps", 0) > 0]
        if not keep:
            return 0
        got = self._read()
        got[model] = (got.get(model, []) + keep)[-self.window:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        durable_write(self.path, json.dumps(got))
        return len(keep)

    def normal(self, model: str, min_calls: int = MIN_CALLS) -> dict | None:
        """{"calls", "tps", "wait_s"}: the medians over the model's healthy calls, None under `min_calls`."""
        pairs = [p for p in self._read().get(model, []) if isinstance(p, list) and len(p) == 2][-self.window:]
        if len(pairs) < min_calls:
            return None
        return {"calls": len(pairs), "tps": _med([p[0] for p in pairs]), "wait_s": _med([p[1] for p in pairs])}


def slow(rows: list[dict], normal: dict | None, factor: float = FACTOR) -> list[str]:
    """Why a cell's calls ran slower than normal, [] when they did not (or when no normal is known).
    The whole cell and its last calls are each checked: a server that slowed late still counts."""
    if not normal or not rows:
        return []
    why = []
    for label, part in (("cell", rows), ("last calls", rows[-max(RECENT, len(rows) // 5):])):
        tps = _med([r["tps"] for r in part if r.get("st") == "ok" and r.get("out", 0) >= MIN_OUT and r.get("tps", 0) > 0])
        wait = _med([r["wait_s"] for r in part])
        if tps and normal["tps"] and tps < normal["tps"] / factor:
            why.append(f"{label}: {tps} tokens/s, normal {normal['tps']} (under 1/{factor:g})")
        if wait > max(normal["wait_s"] * factor, WAIT_FLOOR_S):
            why.append(f"{label}: median server wait {wait} s, normal {normal['wait_s']} s (over {factor:g}x)")
    return why


def refused(bridge: dict) -> bool:
    """Whether the cell's calls met a capacity refusal (429, 503, overload, a model not served) the bridge
    could not wait out: its busy record says it gave up, or the last failed answer was one."""
    b = bridge or {}
    up = b.get("upstream") or {}
    return bool((b.get("busy") or {}).get("gave_up")) or str(b.get("last_fault") or "") in CAPACITY or (
        up.get("status") in CAPACITY_STATUS or str(up.get("code") or "").lower() in CAPACITY)


def classify(stopped: str | None, bridge: dict, normal: dict | None, factor: float = FACTOR) -> list[str]:
    """The reasons a cell is `server`, [] when it is the model's. It needs a stop by the limit or the ceiling,
    or a capacity refusal the bridge gave up on. A cut cell is `server` when its calls ran slower than normal
    (slow); a refusal the bridge gave up on is the server's by itself (it answered that it was full), so it
    counts even before a normal speed is known or when its other calls ran at normal speed."""
    b = bridge or {}
    cut = stopped in ("timeout", "ceiling")
    gave_up = int((b.get("busy") or {}).get("gave_up") or 0)
    if not (cut or gave_up or (refused(b) and not b.get("busy"))):
        return []
    why = slow((b.get("server") or {}).get("per_call") or [], normal, factor)
    if cut and why:
        return [f"hit the {'ceiling' if stopped == 'ceiling' else 'limit'}"] + why
    if gave_up or refused(b):
        waits = (b.get("busy") or {}).get("waits", 0)
        return [f"capacity refusal ({waits} waits, then gave up)" if gave_up else "capacity refusal"] + why
    return []


def mark(row: dict, why: list[str]) -> dict:
    """The row as a `server` cell: not scored, never a fail (the test result stays as `heldout_passed`)."""
    row.update(outcome=OUTCOME, passed=None, server_reason=why)
    row.pop("hang_guard", None)
    return row


def void(row: dict) -> bool:
    """A recheck mark (`was`, from benchrun.recheck_stops) on a run whose server waits were already measured
    (`server`, from judge): the recheck rule covers only runs made before the waits were recorded, so the mark is
    void and the run it marked stands."""
    return row.get("outcome") == OUTCOME and bool(row.get("was")) and bool(row.get("server"))


def refusal_share(row: dict) -> tuple[float, int, int]:
    """(share, refused, calls) of a row's model calls the server refused for capacity (429/503, its call summary's
    `failed_by.capacity` over `calls`); a row given as the bridge record reads its `server` summary the same way."""
    srv = row.get("server") if isinstance(row.get("server"), dict) else {}
    by = srv.get("failed_by") if isinstance(srv.get("failed_by"), dict) else {}
    try:
        calls, refused = int(srv.get("calls") or 0), int(by.get("capacity") or 0)
    except (TypeError, ValueError):
        return 0.0, 0, 0
    return (refused / calls if calls > 0 else 0.0), refused, calls


def refusal_limit(row: dict, default: float | None = None) -> float:
    """The capacity-refusal threshold a row is judged by: the one recorded when it was judged
    (`capacity_refusal_max`), else `default` (`bench.server.capacity_refusal_max`), else REFUSAL_MAX."""
    for v in (row.get("capacity_refusal_max"), default):
        try:
            if v is not None and not isinstance(v, bool) and 0 <= float(v) <= 1:
                return float(v)
        except (TypeError, ValueError):
            pass
    return REFUSAL_MAX


def over_refused(row: dict, limit: float | None = None) -> bool:
    """Mark `row` not run (server) when over `limit` of its calls were capacity refusals: out of
    every denominator, unscored and unflagged (its audit flags move to `unscored_flags`), whatever it scored. A
    cell already not run (no model ran) stays as it is. True when it is such a cell."""
    from . import benchnotrun
    share, refused, calls = refusal_share(row)
    cap = refusal_limit(row, limit)
    if not calls or share <= cap or row.get("outcome") == benchnotrun.OUTCOME:
        return False
    if row.get("outcome") == OUTCOME and row.get("capacity_refusals") is not None:
        return True
    was = row.get("outcome")
    mark(row, [f"{round(share * 100)}% capacity refusals ({refused} of {calls} calls; over {round(cap * 100)}%)"])
    row.update(capacity_refusals=round(share, 3), capacity_refusal_max=cap,
               **({"scored_as": was} if was and was != OUTCOME else {}))
    if row.get("flagged"):
        row["unscored_flags"] = row.pop("flagged")
    return True


def latest(rows, limit: float | None = None) -> dict[tuple, dict]:
    """The last row of each cell (task, rep, arm), void marks skipped; rows without a cell key are skipped. A row
    over its capacity-refusal threshold (over_refused, `limit` for rows that recorded none) comes back marked
    not run (server), as a copy: the file is never rewritten."""
    last: dict = {}
    for r in rows:
        try:
            key = (r["task"], r["rep"], r["arm"])
        except (KeyError, TypeError):
            continue
        if not void(r):
            last[key] = r
    for key, r in last.items():
        if refusal_share(r)[0] > refusal_limit(r, limit) and r.get("capacity_refusals") is None:
            c = dict(r)
            if over_refused(c, limit):
                last[key] = c
    return last


def queue(rows: list[dict]) -> list[dict]:
    """The rerun queue: the latest row of each cell (task, rep, arm) whose outcome is `server`, in order."""
    return [r for r in latest(rows).values() if r.get("outcome") == OUTCOME]


def healthy(probe: dict, normal: dict | None, factor: float = FACTOR) -> bool:
    """A probe result {ok, wait_s, tps}: healthy when it answered at normal speed. With no normal known the
    probe's answer alone decides (it answered)."""
    if not probe.get("ok"):
        return False
    if not normal:
        return True
    if probe.get("tps") and normal["tps"] and probe["tps"] < normal["tps"] / factor:
        return False
    return probe.get("wait_s", 0.0) <= max(normal["wait_s"] * factor, WAIT_FLOOR_S)


def wait_healthy(probe, normal: dict | None, factor: float = FACTOR, every_s: float = 120.0, max_s: float = 3600.0,
                 sleep=time.sleep, clock=time.monotonic) -> tuple[bool, dict]:
    """Probe until the server is healthy: (True, last probe) or (False, last probe) after `max_s`."""
    start, last = clock(), {}
    while True:
        last = probe()
        if healthy(last, normal, factor):
            return True, last
        if clock() - start + every_s > max_s:
            return False, last
        sleep(every_s)


def judge(row: dict, stopped: str | None, bridge: dict, hist: "History", factor: float = FACTOR,
          min_calls: int = MIN_CALLS, scored: bool = True, refusal_max: float | None = None) -> list[str]:
    """Record what the server did to one cell (row's `server`: the call summary, `server_normal`) and mark it
    `server` when classify says so; a cell that is not `scored` (a pass) is never marked by classify. A cell
    over `refusal_max` capacity refusals (over_refused) is marked whatever it scored, a flag or a pass too.
    Healthy calls of an unmarked cell join the model's History. Returns the reasons it was marked, [] when not."""
    b = bridge or {}
    srv = b.get("server") or {}
    if srv:
        row["server"] = {k: v for k, v in srv.items() if k != "per_call"}
    model = str(row.get("model") or "")
    normal = hist.normal(model, min_calls)
    if normal:
        row["server_normal"] = normal
    row["capacity_refusal_max"] = refusal_limit({}, refusal_max)
    if over_refused(row, refusal_max):
        return list(row["server_reason"])
    if scored:
        why = classify(stopped, b, normal, factor)
        if why:
            mark(row, why)
            return why
    if srv.get("per_call") and not slow(srv["per_call"], normal, factor):
        hist.add(model, srv["per_call"])
    return []


PROBE_PROMPT = "Count from 1 to 120, separated by single spaces. Write only the numbers."


def probe(bridge, model: str, prompt: str = PROBE_PROMPT, max_tokens: int = 200, timeout_s: float = 180.0) -> dict:
    """A short health probe through a bench bridge (not yet started; it is started and stopped here): one
    streamed call, timed as a cell's calls are: {ok, wait_s, tps, out}. Never raises; a refusal or an
    outage is {ok: False}."""
    import urllib.request
    try:
        bridge.start()
        req = urllib.request.Request(bridge.url + "/v1/chat/completions", method="POST", data=json.dumps({
            "model": model, "stream": True, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"content-type": "application/json", "authorization": f"Bearer {bridge.token}"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            while r.read(4096):
                pass
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}"}
    finally:
        try:
            bridge.stop()
        except Exception:
            pass
    rows = bridge.calls.export().get("per_call") or []
    ok = [r for r in rows if r.get("st") == "ok"]
    if not ok:
        return {"ok": False, "why": "no answered call"}
    last = ok[-1]
    return {"ok": True, "wait_s": last["wait_s"], "tps": last["tps"], "out": last["out"]}


def describe(row: dict) -> str:
    """How a table shows a cell: `not scored (server)` for a server cell, else its outcome."""
    return NOT_SCORED if row.get("outcome") == OUTCOME else str(row.get("outcome") or "")
