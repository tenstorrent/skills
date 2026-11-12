# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Each project's landing check time: how long its push checks (delivery.push_checks) take before a change lands.

A landing waits for the project's push checks, so they are a smoke suite: under 5 minutes on their host,
aim 4. Slower tests belong in delivery.post_land_checks, which the daemon runs after each landing (postland),
never holding the queue.

- Record: each passing push check run's wall time is one row of LOG under the project's state (record(),
  from push.record_check_s), the newest KEEP rows kept; so is each failing run of a batch (batch.py), since
  it held the landing as long: a slow suite that keeps failing is flagged too.
- Measure (stats()): median and p90 over the last 24 h and over the last LAST_N runs. `ttp status` and the
  daily review show it (status_line()).
- Flag (flag(), each daemon pass, model-free): once the median of the last LAST_N runs is over
  `checks.smoke_max_s` (seconds; default DEFAULT_MAX_S, 0: off), one `gate_slow` observation goes to the
  project's own coordinator per day. It names the slowest tests the newest push log shows (pytest's
  --durations lines) and suggests a smoke split; the project changes its own push checks. With it, one
  priority-1 test speed-up task (labelled SPEED_LABEL, naming those tests) is queued for the project, never
  while another is still open.
- Waits (waits_line()): how long full `ttp checks` runs waited to start: from the run's arrival in the host checks
  queue to its slot's grant (checkcap's WAITS_LOG, the span queuewait's checks marks count), else WAIT_LOG
  (record_wait(), from the run's arrival to its start here or on a spill host) and approval to landed (pushq.landing_times), median and p90
  over the last 24 h: the hours a slow landing spends queued, not testing.
- Subset (subset_flag(), each daemon pass, model-free): while a delivery.push_checks pytest run selects only a
  marked subset (`-m` or `-k`) and delivery.post_land_checks is empty, nothing ever runs the rest of the suite:
  one `checks_subset` observation a day to the project's own coordinator names the fix (set post_land_checks to
  the full suite). `checks.subset_flag` false turns it off.
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
WAIT_LOG = "checks-wait-times.jsonl"
# Whether this release runs delivery.post_land_checks after each landing (postland). While it did not, the
# advice (status_line, flag) never told a coordinator to move tests there: nothing would have run them.
POST_LAND_RUNS = True
KEEP = 200
DAY_S = 86400.0
LAST_N = 10
DEFAULT_MAX_S = 300.0
AIM_S = 240.0
EVENT = "gate_slow"
SPEED_LABEL = "test_speed"   # the daemon's test speed-up task (speed_task), one open at a time
SLOWEST = 10
SUBSET_EVENT = "checks_subset"
# A pytest run that selects a subset: `-m <expr>`, `-k <expr>` (or `-m<expr>`, `-k=<expr>`)
# The selection argument itself, dropped to name the full suite: `-m expr`, `-k "a or b"`, `-m=expr`
_SUBSET_ARG = re.compile(r"\s-[mk](?:[\s=]+|(?=\S))(?:\"[^\"]*\"|'[^']*'|\S+)")
_SUBSET = re.compile(r"\bpytest\b.*?\s-([mk])(?:[\s=]|(?=\S))")
# A pytest --durations line ("12.34s call     tests/x.py::test_y") or a test suite's own slowest-call line
# ("ttp-suite:    61.20s tests/x.py::test_y", the plugin's test runner's)
_DURATION = re.compile(r"^(?:ttp-suite:)?\s*(\d+(?:\.\d+)?)s\s+(?:(?:call|setup|teardown)\s+)?(\S+::\S+)", re.M)


def record(state: Path | None, seconds: float, now: float | None = None, log: str = LOG) -> None:
    """Keep one push check run's wall time (or, in WAIT_LOG, one wait); a failed write only loses a sample."""
    if state is None or seconds is None or seconds < 0 or not seconds and log == LOG:
        return
    path = Path(state) / log
    try:
        if path.exists() and path.stat().st_size > KEEP * 64:
            lines = path.read_text().splitlines()
            if len(lines) > KEEP:
                durable_write(path, "".join(f"{x}\n" for x in lines[-KEEP:]))
        durable_append(path, json.dumps({"ts": time.time() if now is None else now, "s": round(seconds, 1)}) + "\n")
    except OSError:
        pass


def record_wait(state: Path | None, seconds: float, now: float | None = None) -> None:
    """Keep how long one full `ttp checks` run waited from its arrival to its start (0: it started at once)."""
    record(state, max(0.0, seconds), now, WAIT_LOG)


def _rows(state: Path | None, log: str = LOG) -> list[tuple[float, float]]:
    try:
        lines = (Path(state) / log).read_text().splitlines() if state is not None else []
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
            ts, s = float(r["ts"]), float(r["s"])
        except (ValueError, KeyError, TypeError):
            continue
        if s > 0 or s == 0 and log != LOG:
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
    """checks.smoke_max_s in seconds: the project's, else the account's (DEFAULT_MAX_S when unset or
    malformed; 0: no flag)."""
    from . import project
    for got in (cfg, project.load_account_settings()):
        v = ((got or {}).get("checks") or {}).get("smoke_max_s")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            return float(v)
    return DEFAULT_MAX_S


def _m(s: float) -> str:
    return f"{s / 60:.1f} min"


def split_advice(short: bool = False) -> str:
    """How to split slow push checks: into delivery.post_land_checks once this release runs them after
    landing (POST_LAND_RUNS), else keep the slower tests running elsewhere, as nothing runs that list yet."""
    if POST_LAND_RUNS:
        return ("move slow tests to delivery.post_land_checks" if short else
                "Split them: keep a smoke subset in delivery.push_checks and move the slower tests to "
                "delivery.post_land_checks, which run after landing.")
    if short:
        return ("split off a smoke subset only once the slower tests run elsewhere "
                "(delivery.post_land_checks is not run yet)")
    return ("Split them: keep a smoke subset in delivery.push_checks, but this release does not run "
            "delivery.post_land_checks yet, so tests moved there would not run at all: keep the slower "
            "tests running elsewhere (a schedule on the push branch tip) before you shorten push_checks.")


def status_line(state: Path | None, cfg: dict | None, now: float | None = None) -> str:
    """One line for `ttp status`: the landing check time, and whether it is over the smoke limit; '' with no runs."""
    st = stats(state, now)
    if not st["last"]:
        return ""
    parts = [f"{label} median {_m(v['median_s'])}, p90 {_m(v['p90_s'])} ({v['n']} runs)"
             for label, v in (("24 h", st["day"]), ("last runs", st["last"])) if v]
    lim = max_s(cfg)
    over = f"; over the {_m(lim)} smoke limit (checks.smoke_max_s): " + split_advice(short=True) \
        if lim and st["last"]["median_s"] > lim else ""
    return "landing checks: " + "; ".join(parts) + over


def _hm(s: float) -> str:
    return f"{s / 60:.0f} min" if s < 3600 else f"{int(s // 3600)} h {int(s % 3600 // 60):02d} min"


def _slot_waits(state: Path | None, now: float) -> list[float]:
    """The last 24 h's host checks slot waits of ordinary checks runs (checkcap's WAITS_LOG, slot "queue":
    arrival to grant), the span queuewait's checks marks count; [] with none logged (WAIT_LOG then)."""
    from . import checkcap
    if state is None:
        return []
    try:
        recs = checkcap.wait_records(Path(state), now, DAY_S)
    except OSError:
        return []
    return [max(0.0, r["granted_at"] - r["queued_at"]) for r in recs if r.get("slot", "queue") == "queue"]


def waits_line(db, state: Path | None, now: float | None = None, landed: bool = True) -> str:
    """The last 24 h's waits: full checks runs' wait to start and (`landed`) approval to landed, median and
    p90 each; '' with neither."""
    from . import pushq
    now = time.time() if now is None else now
    parts = []
    if w := _summary(_slot_waits(state, now) or [s for ts, s in _rows(state, WAIT_LOG) if now - DAY_S <= ts <= now]):
        parts.append(f"checks to start median {_hm(w['median_s'])}, p90 {_hm(w['p90_s'])} ({w['n']} runs)")
    if landed and db is not None and (lt := pushq.landing_times(db, now - DAY_S))["n"]:
        parts.append(f"approved to landed median {_hm(lt['median_s'])}, p90 {_hm(lt['p90_s'])} ({lt['n']} changes)")
    return ("waits (24 h): " + "; ".join(parts)) if parts else ""


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


def speed_task(db, last: dict, lim: float, tests: list[tuple[float, str]]) -> int:
    """Queue one priority-1 code task to bring the landing checks under `lim` (0: one is still open)."""
    from .db import TERMINAL_TASK_STATES
    marks = ",".join("?" * len(TERMINAL_TASK_STATES))
    if db.one(f"SELECT id FROM tasks WHERE labels LIKE ? AND status NOT IN ({marks})",
              (f'%"{SPEED_LABEL}"%', *TERMINAL_TASK_STATES)):
        return 0
    named = ("\n".join(f"- {k}: {s:.0f}s" for s, k in tests) if tests else
             "- (no durations in the push logs: add --durations=30 to the pytest push check first)")
    spec = (f"urgent: landing checks take a median {_m(last['median_s'])} over the last {last['n']} runs, over "
            f"the {_m(lim)} smoke limit (checks.smoke_max_s); every change waits that long to land.\n\n"
            f"Bring this project's per-change checks (delivery.push_checks, delivery.task_checks) under "
            f"{_m(lim)} on their host, aim {_m(AIM_S)}, without losing coverage:\n"
            f"1. {split_advice()} Keep at least one fast test per area in the smoke subset.\n"
            f"2. Speed up the slowest tests themselves (fixtures, sleeps, polling, repeated setup).\n"
            f"3. Run tests in parallel sized to the host's free cores, slowest first.\n"
            f"Slowest tests in the newest push log:\n{named}\n\n"
            f"Measure the smoke wall time before and after on the same host and report both. If the checks "
            f"are already under the limit when you start, hand off done saying so.")
    return db.add_task("Speed up the landing checks: under " + _m(lim), spec, kind="code", tier="standard",
                       priority=1, origin="daemon", labels=[SPEED_LABEL])


def flag(db, state: Path | None, cfg: dict | None, log_dir: Path | None = None, now: float | None = None) -> str:
    """Raise one `gate_slow` observation a day while the last runs' median is over checks.smoke_max_s, and
    queue a test speed-up task (speed_task) unless one is open; returns its text ('' when none was raised)."""
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
    tid = speed_task(db, last, lim, tests)
    text = (f"Landing checks are slow: median {_m(last['median_s'])} over the last {last['n']} push check runs, "
            f"over the {_m(lim)} smoke limit (checks.smoke_max_s; aim {_m(AIM_S)}). " + named
            + split_advice() + (f" Queued test speed-up task #{tid} (priority 1)." if tid else
                                " A test speed-up task is already open."))
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
         (now, "daemon", "observation", fp, "normal", text[:4000], "queued"))
    return text


def subset_checks(cfg: dict | None) -> list[str]:
    """The delivery.push_checks commands that run only a marked pytest subset (`-m`/`-k`) while
    delivery.post_land_checks is empty; [] otherwise."""
    from . import push
    d = (cfg or {}).get("delivery") or {}
    if push.check_list(d.get("post_land_checks")):
        return []
    return [str(c) for c in push.check_list(d.get("push_checks")) if _SUBSET.search(str(c))]


def subset_flag(db, cfg: dict | None, now: float | None = None, project: str = "<project>") -> str:
    """Raise one `checks_subset` observation a day while the push checks run only a marked pytest subset and
    nothing runs the rest after landing (subset_checks); returns its text ('' when none was raised)."""
    now = time.time() if now is None else now
    if ((cfg or {}).get("checks") or {}).get("subset_flag") is False or not (cmds := subset_checks(cfg)):
        return ""
    fp = f"{SUBSET_EVENT}:{time.strftime('%Y-%m-%d', time.gmtime(now))}"
    if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return ""
    full = [c[:(m := re.search(r"\bpytest\b", c).end())] + _SUBSET_ARG.sub("", c[m:]) for c in cmds]
    text = (f"Push checks run only a marked test subset ({'; '.join(cmds)}) and delivery.post_land_checks is "
            f"empty, so the rest of the suite never runs. Fix: set post_land_checks to the full suite, e.g. "
            f"`ttp config {project} delivery.post_land_checks '{json.dumps([{'run': c} for c in full])}'`. "
            f"(checks.subset_flag false turns this notice off.)")
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
         (now, "daemon", "observation", fp, "normal", text[:4000], "queued"))
    return text
