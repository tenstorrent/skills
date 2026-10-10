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
An llm schedule skipped because the budget gate holds optional work keeps its period open and
retries every half hour (or its own period, if shorter) until the gate allows it.

An llm schedule with a debounce (`debounce_h`; the daily review has 2 h by default) records the
owner/user evidence it was queued on. A trigger within the debounce of that run's successful end,
on the same evidence, is skipped: the review would repeat itself. A failed or cancelled run never
earns it, and evidence that changed (a task moved, the user wrote) runs again. Such a schedule
also keeps its period owed while its previous run is open or its daily budget is used, and
retries every half hour instead of a full period on.

A project may keep its schedules in `harness/schedules.json` (the format of the template's
recurring.json), so every change to them is a commit in the harness. Once that file exists it is
the source of truth: the daemon applies it at start and whenever it changes (a schedule missing
from it is removed), and schedule_set and the web app write their changes back to it. Without the
file the database alone holds them; `ttp schedules NAME --export` creates it once from there.
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
    """'at 09:00 PDT' (the zone's abbreviation at the next run), with the zone's name when a row
    stamped before home zones keeps another zone: 'at 09:00 UTC (UTC)'. '' without an `at`."""
    if not row.get("at"):
        return ""
    tz = zone_of(row, home) or "UTC"
    when = row.get("next_run") or (time.time() if now is None else now)
    pinned = timefmt.valid(row.get("tz")) and timefmt.valid(row.get("tz")) != timefmt.valid(home)
    return f"at {row['at']} {timefmt.abbrev(tz, float(when))}" + (f" ({tz})" if pinned else "")


def upsert(db: DB, name: str, kind: str, every: str | int, at: str | None = None, enabled: bool = True,
           budget_usd_day: float | None = None, description: str = "", payload: dict | None = None,
           home: str | None = None) -> None:
    """Add or change a schedule. A new `at` is read in the project's home zone `home`, and so is
    every later run (the row follows the home zone); an unchanged `at` keeps the zone it had."""
    every_s = parse_every(every)
    at = at or None
    existing = db.one("SELECT * FROM schedules WHERE name=?", (name,))
    same_at = bool(existing) and (existing["at"] or None) == at
    tz = existing.get("tz") if same_at else None
    nxt = existing["next_run"] if existing and existing["next_run"] and same_at else \
        next_run(every_s, at, time.time(), zone_of({"tz": tz}, home))
    db.x("INSERT INTO schedules(name,kind,every_s,at,enabled,budget_usd_day,description,payload,next_run,tz) "
         "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, every_s=excluded.every_s, "
         "at=excluded.at, enabled=excluded.enabled, budget_usd_day=excluded.budget_usd_day, "
         "description=excluded.description, payload=excluded.payload, next_run=excluded.next_run, tz=excluded.tz",
         (name, kind, every_s, at, int(enabled), budget_usd_day, description, json.dumps(payload or {}), nxt, tz))


def due(db: DB, now: float | None = None) -> list[dict]:
    now = now or time.time()
    return db.q("SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=? ORDER BY next_run",
                (now,))


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


def parse_file(text: str | bytes) -> list[dict]:
    """The file's schedules, checked whole: one bad entry rejects the file, so a half-applied
    edit never leaves some schedules changed and others not. Raises ValueError saying what is wrong."""
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
            validate(payload["heal"], f"{where}: payload.heal")
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
        entries = parse_file(raw)
    except ValueError as e:
        return False, f"{FILE}: {e}"
    with db.tx():
        for e in entries:
            upsert(db, e["name"], e["kind"], e["every"], e.get("at") or None, e.get("enabled", True),
                   e.get("budget_usd_day"), e.get("description", ""), e.get("payload", {}), timefmt.home(p))
        names = [e["name"] for e in entries]
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


def debounce_gate(db: DB, name: str, payload: dict, now: float | None = None) -> tuple[str, str | None]:
    """(skip reason or "", the evidence digest to record with a run queued now; None without a debounce).

    Only a run that ended done, within the debounce, on the evidence it was queued on, makes a
    trigger a duplicate. A missing or older record never does."""
    window = debounce_s(name, payload)
    if not window:
        return "", None
    now = time.time() if now is None else now
    digest = evidence(db)
    last = db.kv(DEBOUNCE_KEY + name) or {}
    task = db.task(int(last["task"])) if isinstance(last, dict) and str(last.get("task", "")).isdigit() else None
    if not task or task["status"] != "done" or last.get("evidence") != digest:
        return "", digest
    run = db.one("SELECT MAX(ended) e FROM runs WHERE task=? AND ended IS NOT NULL", (task["id"],))
    ended = float(run["e"]) if run and run["e"] else None
    if ended is None or not 0 <= now - ended < window:
        return "", digest
    return f"skipped: last run done {(now - ended) / 3600:.1f} h ago on unchanged evidence", digest


def record_evidence(db: DB, name: str, task_id: int, digest: str) -> None:
    db.set_kv(DEBOUNCE_KEY + name, {"task": task_id, "evidence": digest})
