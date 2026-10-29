# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Recurring work. Three kinds, cheapest first:
- `command`: a deterministic script (no model). Each stdout line that is a JSON object
  {"text", "severity"} is its own observation; each run of plain lines between them is one.
  Write durations as one number and a unit (52m, 1.5h) or a compound (18h52m): issues are keyed
  with them masked, so a changing age does not open a new issue on every run.
- `watcher`: a built-in probe (pull requests, CI, logs) — also no model.
- `llm`: a scheduled task for a worker (daily review, audits). Budgeted per day, off when the
  governor says optional work must wait.

A schedule that was missed while the machine slept runs once on wake, never once per missed slot.
One that starts a whole period (and at least GAP_MIN_S) late for any other reason, such as the host
or the daemon being down or a tick being held up, also runs once, and the daemon reports the missed
runs and their likely cause (Daemon._schedule_gaps), so a stop never goes unnoticed.
An llm schedule skipped because the budget gate holds optional work keeps its period open and
retries every half hour (or its own period, if shorter) until the gate allows it.

An llm schedule with a debounce (`debounce_h`; the daily review has 2 h by default) records the
owner/user evidence it was queued on. A trigger within the debounce of that run's successful end,
on the same evidence, is skipped: the review would repeat itself. A failed or cancelled run never
earns it, and evidence that changed (a task moved, the user wrote) runs again. Such a schedule
also keeps its period owed while its previous run is open or its daily budget is used, and
retries every half hour instead of a full period on.

An llm schedule may name a `precheck`: a shell command, run off the tick from the project root after
the budget, open-run, debounce and idle gates (`precheck_timeout_s`, 120, capped like a command
watcher's `timeout_s`; see over_caps). Exit 0 skips the trigger as "skipped: precheck: <its first output line>"; any other exit,
a timeout or an error lets the run go ahead and is logged. It judges outside state no model needs to.

A project may keep its schedules in `harness/schedules.json` (the format of the template's
recurring.json), so every change to them is a commit in the harness. Once that file exists it is
the source of truth: the daemon applies it at start and whenever it changes (a schedule missing
from it is removed), and schedule_set and the web app write their changes back to it. Without the
file the database alone holds them; `ttp schedules NAME --export` creates it once from there.

Timeouts have caps (over_caps): a command's `timeout_s` and a precheck's at the daemon's watcher cap
(240 s), a heal block's `timeout_s` there too and its `fix_timeout_s` at 4 h. schedule_set refuses a
value above its cap; one already stored (the file, an older row) runs at the cap, the daemon logs
that once per schedule, and a run cut at the cap says so ("timed out after 240 s (cap; asked 400)").
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .db import DB
from . import timefmt

_EVERY = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_every(spec: str | int) -> int:
    if isinstance(spec, int):
        return spec
    if str(spec).strip().isdigit():   # a bare number is seconds
        return int(str(spec).strip())
    m = _EVERY.match(str(spec))
    if not m:
        raise ValueError(f"bad interval {spec!r}; use e.g. 5m, 1h, 1d or seconds")
    return int(m.group(1)) * _UNIT[m.group(2)]


def next_run(every_s: int, at: str | None, after: float, tz: str | None = None) -> float:
    """Next fire time strictly after `after`. `at` ("HH:MM" in zone `tz`; this machine's zone
    without one) anchors day-scale schedules, at the same wall-clock time across DST changes."""
    if at and every_s >= 86400:
        hh, mm = (int(x) for x in at.split(":"))
        zone = ZoneInfo(timefmt.valid(tz)) if timefmt.valid(tz) else None
        base = datetime.fromtimestamp(after, zone).replace(hour=hh, minute=mm, second=0, microsecond=0)
        while base.timestamp() <= after:
            base += timedelta(seconds=every_s)
        return base.timestamp()
    return after + every_s


def zone_of(row: dict, home: str | None) -> str | None:
    """The zone a schedule's `at` is read in: the one it was stamped with (rows from before home
    zones keep the zone the daemon used then, see db._migrate_schedule_tz), else the project's home zone."""
    return timefmt.valid(row.get("tz")) or timefmt.valid(home)


def at_text(row: dict, home: str | None, now: float | None = None) -> str:
    """'at 09:00 UTC' (the zone's abbreviation at the next run), with the zone's name when a row
    stamped before home zones keeps another zone: 'at 09:00 UTC (UTC)'. '' without an `at`."""
    if not row.get("at"):
        return ""
    tz = zone_of(row, home) or "UTC"
    when = row.get("next_run") or (time.time() if now is None else now)
    pinned = timefmt.valid(row.get("tz")) and timefmt.valid(row.get("tz")) != timefmt.valid(home)
    return f"at {row['at']} {timefmt.abbrev(tz, float(when))}" + (f" ({tz})" if pinned else "")


# Schedule names show in the web app and in commands: letters, digits and . _ - only. A row stored
# under another name before this check keeps working (it can still be changed or switched off).
NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def check_name(name: Any, known: Any = ()) -> None:
    """Raises ValueError unless `name` is a valid schedule name or one of the `known` names already stored."""
    if not (isinstance(name, str) and (NAME_RE.fullmatch(name) or name in known)):
        raise ValueError(f"schedule name {str(name)[:80]!r} is not allowed: use 1-64 letters, digits, '.', '_' or '-'")


def upsert(db: DB, name: str, kind: str, every: str | int, at: str | None = None, enabled: bool = True,
           budget_usd_day: float | None = None, description: str = "", payload: dict | None = None,
           home: str | None = None) -> None:
    """Add or change a schedule. A new `at` is read in the project's home zone `home`, and so is
    every later run (the row follows the home zone); an unchanged `at` keeps the zone it had."""
    existing = db.one("SELECT * FROM schedules WHERE name=?", (name,))
    check_name(name, (name,) if existing else ())
    every_s = parse_every(every)
    at = at or None
    same_at = bool(existing) and (existing["at"] or None) == at
    tz = existing.get("tz") if same_at else None
    keep = existing and existing["next_run"] and same_at and not reenabled(existing, enabled)
    nxt = existing["next_run"] if keep else next_run(every_s, at, time.time(), zone_of({"tz": tz}, home))
    db.x("INSERT INTO schedules(name,kind,every_s,at,enabled,budget_usd_day,description,payload,next_run,tz) "
         "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, every_s=excluded.every_s, "
         "at=excluded.at, enabled=excluded.enabled, budget_usd_day=excluded.budget_usd_day, "
         "description=excluded.description, payload=excluded.payload, next_run=excluded.next_run, tz=excluded.tz",
         (name, kind, every_s, at, int(enabled), budget_usd_day, description, json.dumps(payload or {}), nxt, tz))
    if not enabled:
        _end_mutes(db, [name], OFF, home)


OFF, GONE = "schedule turned off", "schedule removed"


def _end_mutes(db: DB, names: list[str], why: str, home: str | None) -> None:
    """End the mutes of schedules that no longer run: they would mute nothing."""
    from .screen import end_schedule_mutes
    end_schedule_mutes(db, names, why, where=home)


def reenabled(row: dict, enabled: bool, now: float | None = None) -> bool:
    """A disabled schedule turned on again with its next run in the past: it starts from its next
    slot from now, so the time it was off is no missed runs (missed) and no burst of catch-up."""
    return bool(enabled) and not row["enabled"] and float(row["next_run"] or 0) <= (now or time.time())


def set_enabled(db: DB, name: str, enabled: bool, home: str | None = None) -> None:
    """Turn schedule `name` on or off (see reenabled)."""
    row = db.one("SELECT * FROM schedules WHERE name=?", (name,))
    if row is None:
        return
    nxt = next_run(int(row["every_s"]), row["at"], time.time(), zone_of(row, home)) \
        if reenabled(row, enabled) else row["next_run"]
    db.x("UPDATE schedules SET enabled=?, next_run=? WHERE name=?", (int(bool(enabled)), nxt, name))
    if not enabled:
        _end_mutes(db, [name], OFF, home)


def due(db: DB, now: float | None = None) -> list[dict]:
    now = now or time.time()
    return db.q("SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=? ORDER BY next_run",
                (now,))


GAP_MIN_S = 3600   # a schedule starting this late, and at least one whole period late, missed runs


def missed(row: dict, now: float, sleeps: list | None = None) -> int:
    """How many whole periods a due schedule's run starts late: 0 when it is less than a period or
    GAP_MIN_S late, or never ran. Time the host slept or the project was paused (`sleeps`, [start,
    end] pairs, may overlap) is no gap: a schedule missed then runs once after, by design. Anything
    else that long (the host or the daemon down, a tick held up) left a hole in what it records."""
    nxt, every = row.get("next_run"), int(row.get("every_s") or 0)
    if nxt is None or every <= 0 or not row.get("last_run"):
        return 0
    off, end = 0.0, float(nxt)
    for a, b in sorted((max(float(a), float(nxt)), min(float(b), now)) for a, b in sleeps or []):
        if b > max(a, end):
            off, end = off + b - max(a, end), b
    late = now - float(nxt) - off
    return int(late // every) if late >= max(every, GAP_MIN_S) else 0


BUDGET_RETRY_S = 1800


def budget_skipped(status: str | None) -> bool:
    """The budget gate held this run back; it is owed, not done."""
    return bool(status) and status.startswith("skipped: budget ")


def deferred(status: str | None) -> bool:
    """The run is owed but waits on its own open run or its daily budget (debounced schedules)."""
    return bool(status) and status.startswith("deferred: ")


def mark_ran(db: DB, sched: dict, status: str, now: float | None = None, home: str | None = None) -> None:
    now = now or time.time()
    if budget_skipped(status) or deferred(status):
        # Keep last_run so the period still counts as not run, and retry soon instead of a full period on.
        db.x("UPDATE schedules SET last_status=?, next_run=? WHERE name=?",
             (status, now + min(BUDGET_RETRY_S, sched["every_s"]), sched["name"]))
        return
    db.x("UPDATE schedules SET last_run=?, last_status=?, next_run=? WHERE name=?",
         (now, status, next_run(sched["every_s"], sched["at"], now, zone_of(sched, home)),
          sched["name"]))


SKIP_KIND = "schedule_skip"   # events: one record per skipped trigger (record_skip)
SKIP_KEEP = 50                # skip records kept per schedule; the oldest go first


def skip(status: str | None) -> bool:
    """A trigger that did not run: held by the budget gate, its own budget, an open run, a debounce,
    the idle guard or its precheck (every such status starts "skipped" or "deferred")."""
    return bool(status) and status.startswith(("skipped", "deferred"))


def own_budget_skip(status: str | None) -> bool:
    """Held by the schedule's own daily cap (`budget_usd_day`), not by the project's budget gate."""
    return skip(status) and status.endswith(": daily budget used")


def record_skip(db: DB, name: str, status: str, now: float | None = None) -> None:
    """Keep one skipped trigger in the schedule's skip history (events, kind SKIP_KIND), which the
    next run never overwrites, unlike last_status. Capped at SKIP_KEEP per schedule."""
    src = f"schedule-skip:{name}"
    with db.tx():
        db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
             (now or time.time(), src, SKIP_KIND, "low", status[:500], json.dumps({"schedule": name}), "record"))
        db.x("DELETE FROM events WHERE kind=? AND source=? AND id NOT IN "
             "(SELECT id FROM events WHERE kind=? AND source=? ORDER BY id DESC LIMIT ?)",
             (SKIP_KIND, src, SKIP_KIND, src, SKIP_KEEP))


def skips(db: DB, name: str, limit: int = 10, since: float | None = None) -> list[dict]:
    """The schedule's latest skipped triggers, newest first: {"ts", "reason"}."""
    return db.q("SELECT ts, text reason FROM events WHERE kind=? AND source=? AND ts>=? ORDER BY id DESC LIMIT ?",
                (SKIP_KIND, f"schedule-skip:{name}", since or 0, limit))


def run_budget(cfg: dict, s: dict, payload: dict, spent: float | None = None) -> float:
    """One run's dollar cap: payload `budget_usd_run`, else the task default for its tier
    (budget.task_default_usd), never the whole daily cap; and never more than what is left of
    `budget_usd_day` (`spent`: its last-24-h spend)."""
    from .budget import task_default_usd
    own = payload.get("budget_usd_run")
    cap = float(own) if isinstance(own, (int, float)) and not isinstance(own, bool) and own > 0 \
        else task_default_usd(cfg, payload.get("tier") or "standard")
    if s.get("budget_usd_day") is not None:
        cap = min(cap, max(0.0, float(s["budget_usd_day"]) - float(spent or 0)))
    return round(cap, 2)


def failing(status: str | None) -> bool:
    """A run that did nothing because the schedule itself is broken, not because it chose to skip."""
    return bool(status) and (status == "no command" or status.startswith("error"))


def broken(db: DB) -> list[dict]:
    """Enabled schedules whose last run failed, for `ttp status` and the web app."""
    return [r for r in db.q("SELECT name, kind, last_run, last_status FROM schedules WHERE enabled=1 ORDER BY name")
            if failing(r["last_status"])]


def broken_line(db: DB) -> str:
    """The broken schedules in one line, or ""."""
    rows = broken(db)
    return ("schedules failing: " + "; ".join(f"{r['name']} ({r['last_status'][:80]})" for r in rows)) if rows else ""


def waiting_line(db: DB) -> str:
    """Enabled schedules held back by the budget gate, in one line, or ""."""
    rows = [r["name"] for r in db.q("SELECT name, last_status FROM schedules WHERE enabled=1 ORDER BY name")
            if budget_skipped(r["last_status"])]
    return ("schedules waiting for budget: " + ", ".join(rows)) if rows else ""


def spent_today(db: DB, name: str) -> float:
    row = db.one("SELECT COALESCE(SUM(usd),0) s FROM ledger WHERE ts>=? AND source=?",
                 (time.time() - 86400, f"schedule:{name}"))
    return float(row["s"]) if row else 0.0


CAP_RUNS = 10        # finished runs the suggested daily cap takes its median cost from
CAP_MIN_RUNS = 3     # fewer finished runs than this: no suggestion
CAP_MARGIN = 1.25    # suggested cap = runs a day x median run cost x this
CAP_WARN = 0.8       # share of its own daily cap a schedule's spend passes before it is observed


def suggest_cap(db: DB, row: dict, n: int = CAP_RUNS) -> dict | None:
    """An llm schedule's suggested daily cap: its runs a day (from `every`) x the median cost of its
    last `n` finished runs (one scheduled task each) x CAP_MARGIN. None for another kind, or with
    fewer than CAP_MIN_RUNS runs. Only shown: a cap is never changed by itself."""
    if row.get("kind") != "llm" or not int(row.get("every_s") or 0):
        return None
    costs = [float(r["spent_usd"]) for r in db.q(
        "SELECT spent_usd FROM tasks WHERE origin='schedule' AND labels=? AND status IN ('done','failed','cancelled') "
        "AND spent_usd > 0 ORDER BY id DESC LIMIT ?", (json.dumps([row["name"]]), n))]
    if len(costs) < CAP_MIN_RUNS:
        return None
    costs.sort()
    mid = len(costs) // 2
    median = costs[mid] if len(costs) % 2 else (costs[mid - 1] + costs[mid]) / 2
    per_day = 86400 / int(row["every_s"])
    return {"runs_day": round(per_day, 2), "median_usd": round(median, 2), "runs": len(costs),
            "usd": round(per_day * median * CAP_MARGIN, 2)}


def cap_text(s: dict | None) -> str:
    """'suggested cap $6.00 = 24 runs/day x $0.20 median (10 runs) x 1.25', or ''."""
    if not s:
        return ""
    return (f"suggested cap ${s['usd']:.2f} = {s['runs_day']:g} runs/day x ${s['median_usd']:.2f} median "
            f"({s['runs']} runs) x {CAP_MARGIN:g}")


def below_suggestion(db: DB) -> list[str]:
    """Enabled llm schedules whose daily cap is under its suggestion, one line each, for the daily audit."""
    out = []
    for r in db.q("SELECT * FROM schedules WHERE enabled=1 AND kind='llm' AND budget_usd_day IS NOT NULL ORDER BY name"):
        s = suggest_cap(db, r)
        if s and float(r["budget_usd_day"]) < s["usd"] - 0.005:
            out.append(f"{r['name']} cap ${float(r['budget_usd_day']):.2f}/day, {cap_text(s)}")
    return out


def with_costs(db: DB, home: str | None = None) -> list[dict]:
    """Schedules annotated with their last-7-day cost and `at` time, for the web app's recurring pane."""
    rows = db.q("SELECT * FROM schedules ORDER BY name")
    week = time.time() - 7 * 86400
    for r in rows:
        c = db.one("SELECT COALESCE(SUM(usd),0) s, COUNT(*) n FROM ledger WHERE ts>=? AND source=?",
                   (week, f"schedule:{r['name']}"))
        r["cost_7d"] = round(float(c["s"]), 2)
        r["payload"] = json.loads(r["payload"] or "{}")
        r["at_text"] = at_text(r, home)
        r["skips"] = skips(db, r["name"], 5)
        r["skips_24h"] = len(skips(db, r["name"], SKIP_KEEP, time.time() - 86400))
    return rows


# harness/schedules.json ---------------------------------------------------------------------------
FILE = "schedules.json"
_APPLIED = "schedules_file_sha"   # kv: sha256 of the file content last applied or written
_KINDS = ("llm", "command", "watcher")
_KEYS = {"name", "kind", "every", "at", "enabled", "budget_usd_day", "description", "payload"}
_AT = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def file_path(p: Any) -> Path:
    return p.harness / FILE


def _every_text(every_s: int) -> str | int:
    for unit in ("w", "d", "h", "m"):
        if every_s % _UNIT[unit] == 0:
            return f"{every_s // _UNIT[unit]}{unit}"
    return every_s


def entry(row: dict) -> dict:
    """A schedule row as the file writes it: definition only, never its run state."""
    out: dict[str, Any] = {"name": row["name"], "kind": row["kind"], "every": _every_text(int(row["every_s"]))}
    if row["at"]:
        out["at"] = row["at"]
    out["enabled"] = bool(row["enabled"])
    if row["budget_usd_day"] is not None:
        out["budget_usd_day"] = row["budget_usd_day"]
    out["description"] = row["description"] or ""
    payload = row["payload"]
    out["payload"] = json.loads(payload or "{}") if isinstance(payload, str) else (payload or {})
    return out


def over_caps(payload: dict) -> list[tuple[str, float, float]]:
    """(field, asked, cap) for each timeout in a schedule payload above the cap the daemon runs it at:
    `timeout_s`, `precheck_timeout_s`, `heal.timeout_s` and `heal.fix_timeout_s`. A number written as
    text counts (the daemon reads it); other values are left to the payload's own checks."""
    from .heal import FIX_MAX_TIMEOUT_S, MAX_TIMEOUT_S   # MAX_TIMEOUT_S is daemon.WATCHER_MAX_S
    block = payload.get("heal") if isinstance(payload.get("heal"), dict) else {}
    out = []
    for field, v, cap in (("timeout_s", payload.get("timeout_s"), MAX_TIMEOUT_S),
                          ("precheck_timeout_s", payload.get("precheck_timeout_s"), MAX_TIMEOUT_S),
                          ("heal.timeout_s", block.get("timeout_s"), MAX_TIMEOUT_S),
                          ("heal.fix_timeout_s", block.get("fix_timeout_s"), FIX_MAX_TIMEOUT_S)):
        try:
            asked = float(v) if not isinstance(v, bool) and v is not None else 0.0
        except (TypeError, ValueError):
            continue
        if asked > cap:
            out.append((field, asked, cap))
    return out


def over_cap_text(over: list[tuple[str, float, float]]) -> str:
    """over_caps' findings in words: "`timeout_s` 400 is above its cap of 240 s"."""
    return "; ".join(f"`{field}` {asked:g} is above its cap of {cap:g} s" for field, asked, cap in over)


def capped_note(asked: Any, cap: float) -> str:
    """" (cap; asked 400)" after a "timed out after 240 s" when the cap cut the asked timeout, else ""."""
    try:
        return f" (cap; asked {float(asked):g})" if float(asked) > cap else ""
    except (TypeError, ValueError):
        return ""


def parse_file(text: str | bytes, known: Any = ()) -> list[dict]:
    """The file's schedules, checked whole: one bad entry rejects the file, so a half-applied
    edit never leaves some schedules changed and others not. Raises ValueError saying what is wrong.
    `known`: names already stored, kept even when they predate the name check (check_name)."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"not valid JSON ({e})") from None
    if not isinstance(data, list):
        raise ValueError("must be a JSON list of schedules")
    seen: set[str] = set()
    for i, e in enumerate(data):
        where = f"entry {i + 1}"
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"].strip():
            raise ValueError(f"{where} needs a `name`")
        name = e["name"]
        where = f"schedule {name!r}"
        try:
            check_name(name, known)
        except ValueError as err:
            raise ValueError(f"entry {i + 1}: {err}") from None
        if name in seen:
            raise ValueError(f"{where} appears twice")
        seen.add(name)
        unknown = sorted(set(e) - _KEYS)
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}; use {', '.join(sorted(_KEYS))}")
        if e.get("kind") not in _KINDS:
            raise ValueError(f"{where}: `kind` must be one of {', '.join(_KINDS)}")
        if "every" not in e:
            raise ValueError(f"{where} needs `every` (e.g. 30m, 1d)")
        try:
            if parse_every(e["every"]) <= 0:
                raise ValueError("an interval must be positive")
        except (ValueError, TypeError) as err:
            raise ValueError(f"{where}: {err}") from None
        if e.get("at") is not None and not (isinstance(e["at"], str) and _AT.match(e["at"])):
            raise ValueError(f"{where}: `at` is a time HH:MM in the project's home zone")
        if not isinstance(e.get("enabled", True), bool):
            raise ValueError(f"{where}: `enabled` is true or false")
        b = e.get("budget_usd_day")
        if b is not None and (isinstance(b, bool) or not isinstance(b, (int, float)) or b < 0):
            raise ValueError(f"{where}: `budget_usd_day` is a number of dollars or null")
        if not isinstance(e.get("description", ""), str):
            raise ValueError(f"{where}: `description` is text")
        payload = e.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError(f"{where}: `payload` is an object")
        if "heal" in payload:
            if e["kind"] != "command":
                raise ValueError(f"{where}: only a command schedule takes `payload.heal`")
            from .heal import validate
            validate(payload["heal"], f"{where}: payload.heal", parse_every(e["every"]))
        if payload.get("precheck") is not None:
            if e["kind"] != "llm":
                raise ValueError(f"{where}: only an llm schedule takes `payload.precheck`")
            if not isinstance(payload["precheck"], str):
                raise ValueError(f"{where}: `payload.precheck` is a shell command")
        r = payload.get("budget_usd_run")
        if r is not None and (isinstance(r, bool) or not isinstance(r, (int, float)) or r <= 0):
            raise ValueError(f"{where}: `payload.budget_usd_run` is a positive number of dollars or absent")
        if r is not None and e["kind"] != "llm":
            raise ValueError(f"{where}: only an llm schedule takes `payload.budget_usd_run`")
        t = payload.get("precheck_timeout_s")
        if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0):
            raise ValueError(f"{where}: `payload.precheck_timeout_s` is a number of seconds")
        if e["kind"] == "command" and e.get("enabled", True) and not str(payload.get("command") or "").strip() \
                and not payload.get("heal"):
            raise ValueError(f"{where}: an enabled command schedule needs `payload.command` or `payload.heal`")
    return data


def _sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def sync_file(p: Any, force: bool = False) -> tuple[bool, str | None]:
    """Apply harness/schedules.json to the database if it changed since it was last applied (or
    always, with force). Returns (applied, problem): a file that does not check out is left
    unapplied and the database keeps the schedules it had."""
    path = file_path(p)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False, None
    except OSError as e:
        return False, f"{FILE}: {e}"
    db, sha = p.db, _sha(raw)
    if not force and db.kv(_APPLIED) == sha:
        return False, None
    try:
        entries = parse_file(raw, {r["name"] for r in db.q("SELECT name FROM schedules")})
    except ValueError as e:
        return False, f"{FILE}: {e}"
    from . import selfreview
    with db.tx():
        # A file edit that changes daily-review's state is the user's record of the switch: the daemon
        # cannot tell who committed it, and the coordinator, web app and self-review check record their
        # own switches and write the file in step, so their re-applies (each daemon start forces one)
        # change nothing here and leave the record as it was. Accepted gap: a harness worker's own edit
        # of the file counts as the user's word.
        row = db.one("SELECT enabled FROM schedules WHERE name=?", (selfreview.NAME,))
        for e in entries:
            upsert(db, e["name"], e["kind"], e["every"], e.get("at") or None, e.get("enabled", True),
                   e.get("budget_usd_day"), e.get("description", ""), e.get("payload", {}), timefmt.home(p))
        names = [e["name"] for e in entries]
        mine = [bool(e.get("enabled", True)) for e in entries if e["name"] == selfreview.NAME]
        if row and not mine:
            selfreview.record_off(db, selfreview.NAME, None, f"harness/{FILE}", True)
        elif mine and (row is None or mine[0] != bool(row["enabled"])):
            selfreview.record_off(db, selfreview.NAME, mine[0], f"harness/{FILE}", True)
        _end_mutes(db, [r["name"] for r in db.q("SELECT name FROM schedules") if r["name"] not in names], GONE,
                   timefmt.home(p))
        db.x(f"DELETE FROM schedules WHERE name NOT IN ({','.join('?' * len(names))})", names)
        db.set_kv(_APPLIED, sha)
    return True, None


def held_for_harness_task(p: Any) -> bool:
    """True while harness/schedules.json has uncommitted changes and a harness task is running:
    that worker may not have committed the script a new entry runs yet, so the edit waits until
    it is committed or no harness task runs (then the daemon applies and commits it)."""
    if not (p.harness / ".git").exists() or not file_path(p).exists():
        return False
    if not p.db.one("SELECT r.id FROM runs r JOIN tasks t ON t.id=r.task "
                    "WHERE r.status='running' AND t.kind='harness' LIMIT 1"):
        return False
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(p.harness), "status", "--porcelain", "--", FILE],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def before_change(p: Any) -> None:
    """Called before a schedule change that writes back to the file: applies a hand edit the
    daemon has not picked up yet, so the write does not undo it, and refuses while the file is broken."""
    _, problem = sync_file(p)
    if problem:
        raise ValueError(f"{problem}; fix harness/{FILE} before changing schedules")


def write_file(p: Any, message: str, create: bool = False) -> bool:
    """Write the database's schedules to harness/schedules.json and commit it. Only when the
    project keeps the file, unless `create` (the one-time export). Returns whether it wrote."""
    path = file_path(p)
    if not (create or path.exists()):
        return False
    db = p.db
    text = json.dumps([entry(r) for r in db.q("SELECT * FROM schedules ORDER BY name")], indent=2) + "\n"
    from .project import durable_write
    durable_write(path, text)
    db.set_kv(_APPLIED, _sha(text))
    p.commit_harness([path], message)
    return True


# Debounce of unchanged completions ------------------------------------------------------------
DEBOUNCE_KEY = "schedule_evidence:"   # kv per schedule: {"task", "evidence"} of the run last queued
DEFAULT_DEBOUNCE_H = {"daily-review": 2.0}


def debounce_s(name: str, payload: dict) -> float:
    """The schedule's debounce in seconds; 0 when it has none (`debounce_h` null or 0 turns it off)."""
    hours = payload.get("debounce_h", DEFAULT_DEBOUNCE_H.get(name))
    try:
        return max(0.0, float(hours) * 3600) if hours is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def evidence(db: DB) -> str:
    """A digest of what a review looks at: the tasks the owner and user care about (not schedules'
    own runs) with their outcome, constraints and deferrals, and the last message from the user.
    Spend and timestamps are left out, so accounting alone never counts as a change."""
    tasks = db.q("SELECT id, status, spec, result, blocked_reason, depends_on, not_before FROM tasks "
                 "WHERE origin!='schedule' ORDER BY id")
    user = db.one("SELECT MAX(id) id FROM messages WHERE direction='in'")
    return _sha(json.dumps([tasks, user and user["id"]], sort_keys=True, default=str))


def outcome(db: DB) -> str:
    """The part of the evidence only an owner or the user moves: the task set with each task's status,
    spec, result and dependencies, and the last message from the user. The daemon rewrites not_before
    and blocked_reason model-free on every waiting probe ("next try ..."), so they are left out: a
    quiet project with a waiting task must not owe a review every day."""
    tasks = db.q("SELECT id, status, spec, result, depends_on FROM tasks WHERE origin!='schedule' ORDER BY id")
    user = db.one("SELECT MAX(id) id FROM messages WHERE direction='in'")
    return _sha(json.dumps([tasks, user and user["id"]], sort_keys=True, default=str))


def debounce_gate(db: DB, name: str, payload: dict,
                  now: float | None = None) -> tuple[str, dict | None, str]:
    """(skip reason or "", the digests to record with a run queued now (None without a debounce),
    why a run is owed: "changed", "not-done", or "" when only the idle guard can say).

    Only a run that ended done, within the debounce, on the evidence it was queued on, makes a
    trigger a duplicate. A missing or older record never does. A changed outcome, or a last run
    that did not end done, owes a run whatever the idle guard sees; evidence that moved only in
    what the daemon rewrites model-free is no duplicate but leaves the call to the idle guard."""
    window = debounce_s(name, payload)
    if not window:
        return "", None, ""
    now = time.time() if now is None else now
    digests = {"evidence": evidence(db), "outcome": outcome(db)}
    last = db.kv(DEBOUNCE_KEY + name) or {}
    task = db.task(int(last["task"])) if isinstance(last, dict) and str(last.get("task", "")).isdigit() else None
    if not task:
        return "", digests, ""
    # A record from before the outcome digest falls back to the full evidence (owes one run at most).
    key = "outcome" if "outcome" in last else "evidence"
    if last.get(key) != digests[key]:
        return "", digests, "changed"
    if task["status"] != "done":
        return "", digests, "not-done"
    run = db.one("SELECT MAX(ended) e FROM runs WHERE task=? AND ended IS NOT NULL", (task["id"],))
    ended = float(run["e"]) if run and run["e"] else None
    if last.get("evidence") != digests["evidence"] or ended is None or not 0 <= now - ended < window:
        return "", digests, ""
    return f"skipped: last run done {(now - ended) / 3600:.1f} h ago on unchanged evidence", digests, ""


def record_evidence(db: DB, name: str, task_id: int, digests: dict) -> None:
    db.set_kv(DEBOUNCE_KEY + name, {"task": task_id, **digests})
