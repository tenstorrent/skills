# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The account's day and the global daily cap: what every tt-project project billed to one account
has spent today, on every machine tt-project can see.

Once `budget.day_start` ("HH:MM") is set, the day is fixed: it starts at that time in
`budget.timezone` (an IANA zone, default UTC; never the host's own zone, since servers run on UTC)
and lasts until the same wall-clock time the next day, so it is 23 or 25 hours long across a
daylight-saving change. The default, an empty `day_start`, keeps the rolling 24 hours. Weekly caps
stay rolling 7 days. `budget.global_daily_usd` defaults to $200 (0 = off).

The global total for a provider counts, for this day:
- this machine: every project in the registry (`ttp list`) whose host is this machine, read from its
  database read-only, plus running work as last priced;
- other machines: the hosts of the registry's projects on other machines, and machines-list entries
  tagged `tt-project`, reached by ssh (BatchMode, as `ttp` already reaches them) running
  `ttp spend-today` there, which sums only that machine's own projects. Each machine's answers are
  cached in ~/.tt-project/global-spend.json, one per window (projects on one machine may count
  different days), with when each was read. A machine that cannot be reached keeps its last answer
  for the same day, which still counts and is shown as stale;
- machines that cannot be reached from here but can reach this machine (a laptop that can reach a
  server but not the other way round): they push their own `spend-today` answers over ssh into
  `ttp spend-today --receive` here (push, receive). Pushed answers are kept per machine and window
  in ~/.tt-project/global-spend-pushed.json and count like pulled ones: aged by when they arrived,
  stale past STALE_S. A machine both pulled and pushed counts once, with its freshest answer;
- each other machine's own other Claude Code sessions: every answer, pulled or pushed, carries the
  sending machine's estimate of them (localspend) as `other_sessions`, beside its projects' rows. It
  counts once per machine with that machine's answer, so never twice. An answer from an older
  tt-project has no such field: it counts 0 for them and the total says those machines' sessions are
  not in it;
- other spend sources registered with `add_other_source` (a hook for spend outside tt-project, e.g.
  the user's own sessions; localspend adds this machine's other Claude Code sessions).

Only spend on the same provider and the same account counts: a Codex project, or a project logged
in to another account, is not billed with this one. Rows with no account recorded count (fail safe).
Only billed spend counts (billing.py): spend a plan paid for, e.g. before a switch to usage billing,
stays out, as it does for the project caps.

The settings live in the account-level file ~/.tt-project/settings.json (`ttp config --account KEY
VALUE`), which every project on the machine reads; a project's own project.json may override them.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import billing, project

DAY = 86400.0
REFRESH_S = 600            # each other machine is asked again this long after the last try
STALE_S = 1800             # an answer older than this, or a failed last try, is stale
LOCAL_CACHE_S = 30         # this machine's other projects are read again after this long
TAG = "tt-project"         # machines-list tag of a machine that runs tt-project projects
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
REMOTE_TTP = "~/.tt-project/lib/current/bin/ttp"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
PUSH_TIMEOUT_S = 30        # one push over ssh
PUSH_BACKOFF_S = (REFRESH_S, 3600)   # after a failed push: first wait, doubling up to the last
PUSH_PULLED_S = 6 * 3600   # a machine that already reads this one itself is pushed to this rarely
PUSHED_KEEP_S = 2 * DAY    # a machine that pushed nothing for this long is forgotten
RECEIVE_BYTES = 64 << 10   # the most `ttp spend-today --receive` reads
MAX_WINDOWS, MAX_ROWS, MAX_PROJECTS, MAX_PUSHERS = 4, 64, 200, 32
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,79}$")
PROVIDER_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
KEY_RE = re.compile(r"^[0-9a-f]{16}$")
SESSIONS = "other_sessions"   # an answer's field: the machine's other Claude Code sessions, estimated

# Extra spend sources: fn(provider, account, start, end) -> (usd, label). Each adds its dollars to the
# global total and its label to what the total says it includes. A source that fails is left out and
# named in the total's errors.
OTHER_SOURCES: list[Callable[[str, str, float, float], tuple[float, str]]] = []


def add_other_source(fn: Callable[[str, str, float, float], tuple[float, str]]) -> None:
    if fn not in OTHER_SOURCES:
        OTHER_SOURCES.append(fn)


# the day --------------------------------------------------------------------------------------
def zone(budget: dict) -> tuple[ZoneInfo, str]:
    """The budget's zone and its name; an unknown name falls back to UTC."""
    name = str(budget.get("timezone") or "UTC")
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC"), "UTC"


def day_bounds(budget: dict, now: float) -> tuple[float, float] | None:
    """(start, end) of the budget day containing `now`, or None when `day_start` is empty (rolling
    24 h). An unreadable `day_start` counts as 00:00."""
    raw = budget.get("day_start")
    if raw in (None, "", False):
        return None
    m = TIME_RE.match(str(raw).strip())
    at = dtime(int(m.group(1)), int(m.group(2))) if m else dtime(0, 0)
    tz, _ = zone(budget)
    local = datetime.fromtimestamp(now, tz)
    d = local.date()
    if (local.hour, local.minute, local.second) < (at.hour, at.minute, 0):
        d -= timedelta(days=1)

    def stamp(day: date) -> float:
        return datetime.combine(day, at, tzinfo=tz).timestamp()
    return stamp(d), stamp(d + timedelta(days=1))


def window(budget: dict, now: float) -> tuple[float, float, bool]:
    """(start, end, rolling) of the global total's window. A budget day when `day_start` is set;
    otherwise the rolling 24 h, with its start floored to REFRESH_S so it stays put across daemon
    ticks (the refresh, the caches and the remote answers compare by it) and counts up to REFRESH_S
    more than 24 h (fail safe). The rolling end lies a day ahead: spend is never in the future."""
    day = day_bounds(budget, now)
    if day:
        return day[0], day[1], False
    start = (now - DAY) // REFRESH_S * REFRESH_S
    return start, start + 2 * DAY, True


def setting_problems(budget: dict) -> list[str]:
    out = []
    raw = budget.get("day_start")
    if raw not in (None, "") and not TIME_RE.match(str(raw).strip()):
        out.append(f"budget.day_start: {raw!r} is not HH:MM; the day starts at 00:00")
    if "timezone" in budget and zone(budget)[1] != str(budget.get("timezone") or "UTC"):
        out.append(f"budget.timezone: {budget['timezone']!r} is not an IANA time zone; UTC is used")
    try:
        if float(budget.get("global_daily_usd") or 0) < 0:
            raise ValueError
    except (TypeError, ValueError):
        out.append(f"budget.global_daily_usd: {budget.get('global_daily_usd')!r} is not a dollar amount >= 0")
    v = budget.get("push_spend_to")
    if v not in (None, "none") and not (isinstance(v, list) and all(isinstance(x, str) and NAME_RE.match(x) for x in v)):
        out.append(f"budget.push_spend_to: {v!r} is not a list of ssh aliases or \"none\"")
    return out


# who pays ---------------------------------------------------------------------------------------
def account_key(provider: str, account: str) -> str:
    """What two machines compare to tell the same account: a hash, so the account's name stays put."""
    return hashlib.sha256(f"{provider}\0{account or ''}".encode()).hexdigest()[:16]


def account_of(provider: str) -> str:
    try:
        from .providers import get_provider
        return get_provider(provider).account() or ""
    except Exception:
        return ""


def _matches(row: dict, provider: str, account: str) -> bool:
    """Same provider and the same account; a row without an account counts, and when this machine
    does not know its own account every row of the provider counts (both fail safe)."""
    if row["provider"] != provider:
        return False
    return not account or row["key"] in (account_key(provider, account), account_key(provider, ""))


# this machine -----------------------------------------------------------------------------------
def _rows(conn: sqlite3.Connection, start: float, end: float) -> list[dict]:
    """Billed spend per (provider, account key) in [start, end), with running work as last priced:
    the same rule as the project caps (billing.billed_by_account), so plan spend never counts."""
    out: dict[tuple, float] = {}
    for (prov, acct), usd in billing.billed_by_account(conn, start, end, running_at=time.time()).items():
        k = (prov, account_key(prov, acct))
        out[k] = out.get(k, 0.0) + usd
    return [{"provider": p, "key": k, "usd": round(u, 4)} for (p, k), u in out.items()]


def connect_ro(path: Path, timeout: float = 5) -> sqlite3.Connection:
    """Another project's database, opened without writing a byte of it. While it has a -wal file (its
    daemon or a command has it open) as an ordinary reader (mode=ro); otherwise as immutable, since
    even a read-only reader of a WAL database creates its -wal and -shm files."""
    path = Path(path).absolute()
    wal = path.with_name(path.name + "-wal").exists()
    return sqlite3.connect(f"{path.as_uri()}?{'mode=ro' if wal else 'immutable=1'}", uri=True, timeout=timeout)


def _read_only(path: Path, start: float, end: float) -> list[dict]:
    conn = connect_ro(path)
    try:
        return _rows(conn, start, end)
    finally:
        conn.close()


def local_projects(skip: str | None = None) -> list[tuple[str, Path]]:
    """(name, database) of the registry's projects on this machine, but the one at `skip`."""
    here = project.hostname()
    out = []
    for name, e in sorted((project.load_registry().get("projects") or {}).items()):
        if not isinstance(e, dict) or e.get("host") not in (None, here) or not e.get("dir"):
            continue
        db = Path(e["dir"]) / project.FOLDER / "state" / "project.db"
        if db.is_file() and (skip is None or str(db.resolve()) != skip):
            out.append((name, db))
    return out


_LOCAL: dict[tuple, tuple[float, float, float, dict]] = {}   # (home, skip) -> (read at, start, end, totals)


def machine_totals(start: float, end: float, skip: str | None = None, now: float | None = None,
                   cached: bool = False) -> dict:
    """This machine's projects' spend in [start, end), by provider and account key. `skip` is the
    database of the asking project, which counts its own."""
    now = now or time.time()
    key = (str(project.HOME_DIR), skip)
    hit = _LOCAL.get(key)
    if cached and hit and hit[1:3] == (start, end) and now - hit[0] < LOCAL_CACHE_S:
        return hit[3]
    rows: dict[tuple, float] = {}
    names, errors = [], []
    for name, db in local_projects(skip):
        try:
            got = _read_only(db, start, end)
        except sqlite3.Error as e:
            errors.append(f"{name}: {e}")
            continue
        names.append(name)
        for r in got:
            k = (r["provider"], r["key"])
            rows[k] = rows.get(k, 0.0) + r["usd"]
    out = {"host": project.hostname(), "start": start, "end": end, "projects": names, "errors": errors,
           "rows": [{"provider": p, "key": k, "usd": round(u, 4)} for (p, k), u in rows.items()]}
    if cached:                      # one entry per caller, which asks with the same window each tick
        _LOCAL[key] = (now, start, end, out)
    return out


def answer(start: float, end: float, now: float | None = None) -> dict:
    """What this machine tells another one about [start, end) (`ttp spend-today`, pulled or pushed):
    machine_totals, plus its other Claude Code sessions as SESSIONS ({provider, key, usd, sessions,
    estimated: true}), or None there while they are unknown (the local logs not read yet)."""
    out = dict(machine_totals(start, end, now=now))
    try:
        from . import localspend
        out[SESSIONS] = localspend.field(start, end, now)
    except Exception:
        out[SESSIONS] = None
    return out


def _sessions_ok(x) -> bool:
    """An answer's SESSIONS value: None (unknown there), or one account's estimate within range."""
    return x is None or (isinstance(x, dict) and isinstance(x.get("provider"), str) and PROVIDER_RE.match(x["provider"])
                         and isinstance(x.get("key"), str) and KEY_RE.match(x["key"])
                         and _num(x.get("usd")) and 0 <= x["usd"] < 1e6
                         and (x.get("sessions") is None or _num(x["sessions"]) and 0 <= x["sessions"] < 1e6))


def _sessions(x) -> dict | None:
    """A checked SESSIONS value, as kept."""
    return None if x is None else {"provider": x["provider"], "key": x["key"], "usd": float(x["usd"]),
                                   "sessions": int(x.get("sessions") or 0), "estimated": True}


# other machines ---------------------------------------------------------------------------------
def cache_path() -> Path:
    return project.HOME_DIR / "global-spend.json"


def load_cache() -> dict:
    try:
        data = json.loads(cache_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def targets() -> list[str]:
    """ssh targets of the other machines: the registry's projects elsewhere, and machines-list
    entries tagged `tt-project`, this machine left out."""
    here = project.hostname()
    out = {e.get("ssh") or e["host"] for e in (project.load_registry().get("projects") or {}).values()
           if isinstance(e, dict) and e.get("host") and e["host"] != here}
    try:
        from . import machines
        known = machines.load()
        mine = machines.here(known)
        out |= {alias for alias, m in known.items()
                if TAG in machines.tag_list(m.get("tags")) and not (mine and mine[0] == alias)
                and (m.get("hostname") or alias) != here}
    except Exception:
        pass
    return sorted(out)


def _whole(data) -> bool:
    """An answer total() can count: the host's name, a list of projects and rows of a provider, an
    account key and a finite dollar amount, as `ttp spend-today` gives, and a well-formed SESSIONS
    value if it has one."""
    rows = data.get("rows") if isinstance(data, dict) else None
    return (isinstance(rows, list) and isinstance(data.get("host") or "", str)
            and isinstance(data.get("projects") or [], list) and _sessions_ok(data.get(SESSIONS))
            and all(isinstance(r, dict) and {"provider", "key"} <= r.keys() and isinstance(r.get("usd") or 0, (int, float))
                    and math.isfinite(r.get("usd") or 0) for r in rows))


def fetch(target: str, start: float, end: float) -> dict:
    """Ask another machine for its projects' spend in [start, end). Raises on any failure, a partial
    or malformed answer included."""
    cmd = f"{REMOTE_TTP} spend-today --since {start:.0f} --until {end:.0f} --json"
    r = subprocess.run([*SSH, target, cmd], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(((r.stderr or "").strip().splitlines() or [f"exit {r.returncode}"])[-1][:200])
    data = json.loads(r.stdout)
    if not _whole(data):
        raise RuntimeError("unexpected answer")
    return data


def _key(start: float, end: float) -> str:
    """The cache key of a window's answer. All rolling windows share one: their start moves every
    REFRESH_S and an answer counts by its age alone. A budget day is keyed by its start. A rolling
    window spans two days (window()), a budget day at most 25 h."""
    return "rolling" if end - start > 1.5 * DAY else f"{start:.0f}"


def _windows(m: dict) -> dict:
    """A machine's answers by window key; a cache entry from before they were kept per window holds
    one answer at its top level."""
    if isinstance(m.get("windows"), dict):
        return m["windows"]
    if m.get("rows") is not None and m.get("start") is not None and m.get("end") is not None:
        return {_key(float(m["start"]), float(m["end"])): {k: m[k] for k in ("ts", "start", "end", "rows", "projects")
                                                           if k in m}}
    return {}


def _due(m: dict, start: float, end: float, now: float) -> bool:
    """Whether to ask a machine whose cache entry is `m` about [start, end): REFRESH_S after its
    answer for that window, or at once when it has none or one for an earlier rolling start. A failed last try holds every window off by
    its time alone, so a machine that is down is asked once per REFRESH_S, not on every daemon tick.
    Answers are kept per window, so projects on this machine that count different days do not make
    each other ask again. A time stamped after `now` (the clock went back) holds nothing off."""
    if not m.get("ok") and 0 <= now - float(m.get("tried") or 0) < REFRESH_S:
        return False
    w = _windows(m).get(_key(start, end)) or {}
    return not 0 <= now - float(w.get("ts") or 0) < REFRESH_S or w.get("start") != start


def refresh(start: float, end: float, now: float | None = None, force: bool = False) -> dict:
    """Ask every other machine that is due, and save what each said with its time. One process at a
    time does this (a lock in ~/.tt-project); the others read the cache."""
    now = now or time.time()
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    with open(project.HOME_DIR / "global-spend.lock", "a") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return load_cache()
        cache = load_cache()
        machines = cache.setdefault("machines", {})
        for t in targets():
            m = machines.get(t) or {}
            wins = {k: w for k, w in _windows(m).items() if float(w.get("end") or 0) > now}   # past days go
            base = {k: m[k] for k in ("host", "tried", "ts", "ok", "error") if k in m}
            machines[t] = {**base, "windows": wins}
            if not force and not _due(m, start, end, now):
                continue
            try:
                got = fetch(t, start, end)
            except Exception as e:
                machines[t].update(tried=now, ok=False, error=str(e)[:200])
                continue
            wins[_key(start, end)] = {"ts": now, "start": start, "end": end, "rows": got["rows"],
                                      "projects": got.get("projects") or [],
                                      **({SESSIONS: _sessions(got[SESSIONS])} if SESSIONS in got else {})}
            machines[t] = {"host": got.get("host") or t, "tried": now, "ts": now, "ok": True, "windows": wins}
        for gone in set(machines) - set(targets()):
            del machines[gone]
        project.write_json(cache_path(), cache, mode=0o600)
        return cache


_THREAD: dict[str, threading.Thread] = {}
_FAILED = {"at": 0.0, "push": 0.0}   # when this process's last background refresh (or push) failed


def refresh_async(budget: dict, now: float | None = None) -> None:
    """Start a refresh in the background when one is due and none is running: ssh may take seconds."""
    now = now or time.time()
    if float(budget.get("global_daily_usd") or 0) <= 0 or 0 <= now - _FAILED["at"] < REFRESH_S:
        return
    t = _THREAD.get("t")
    if t and t.is_alive():
        return
    start, end, _ = window(budget, now)
    cache = (load_cache().get("machines") or {})
    pull = any(_due(cache.get(x) or {}, start, end, now) for x in targets())
    send = not 0 <= now - _FAILED["push"] < REFRESH_S and _push_due(now)
    if not pull and not send:
        return

    def work():
        if pull:
            _refresh_quietly(start, end, now)
        if send:
            try:
                push(budget, now)
            except Exception:
                _FAILED["push"] = time.time()
    t = threading.Thread(target=work, daemon=True)
    _THREAD["t"] = t
    t.start()


def _refresh_quietly(start: float, end: float, now: float) -> None:
    """refresh() in the background. When it fails (say the disk is full and the cache cannot be
    saved), its tries may be on record nowhere: this process waits REFRESH_S before the next."""
    try:
        refresh(start, end, now)
    except Exception:
        _FAILED["at"] = time.time()


# pushed answers ----------------------------------------------------------------------------------
def pushed_path() -> Path:
    return project.HOME_DIR / "global-spend-pushed.json"


def push_state_path() -> Path:
    return project.HOME_DIR / "global-spend-push.json"


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _flock(name: str, wait: bool) -> int | None:
    """A guard file in ~/.tt-project held with flock: its descriptor, or None when another process
    holds it and `wait` is off. The caller closes it."""
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(project.HOME_DIR / name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _valid_push(rec, now: float) -> dict | None:
    """A pushed record as {host, sent, windows: {key: answer}}, or None if it is not one: this
    machine's own name, an unknown field type, an amount out of range or anything over the caps
    rejects the whole record. Windows already over are dropped."""
    if not isinstance(rec, dict) or rec.get("v") != 1 or not _num(rec.get("sent")):
        return None
    host, wins = rec.get("host"), rec.get("windows")
    if not isinstance(host, str) or not NAME_RE.match(host) or host == project.hostname():
        return None
    if not isinstance(wins, list) or not 1 <= len(wins) <= MAX_WINDOWS:
        return None
    out = {}
    for w in wins:
        if not isinstance(w, dict) or not (_num(w.get("start")) and _num(w.get("end"))):
            return None
        start, end, rows, names = float(w["start"]), float(w["end"]), w.get("rows"), w.get("projects", [])
        if not 0 < end - start <= 2 * DAY or not isinstance(rows, list) or len(rows) > MAX_ROWS:
            return None
        if not isinstance(names, list) or len(names) > MAX_PROJECTS or not all(
                isinstance(n, str) and NAME_RE.match(n) for n in names):
            return None
        if not _sessions_ok(w.get(SESSIONS)):
            return None
        for r in rows:
            if not (isinstance(r, dict) and isinstance(r.get("provider"), str) and PROVIDER_RE.match(r["provider"])
                    and isinstance(r.get("key"), str) and KEY_RE.match(r["key"])
                    and _num(r.get("usd")) and 0 <= r["usd"] < 1e6):
                return None
        if end > now:
            out[_key(start, end)] = {"ts": now, "start": start, "end": end, "projects": names,
                                     "rows": [{"provider": r["provider"], "key": r["key"], "usd": float(r["usd"])}
                                              for r in rows],
                                     **({SESSIONS: _sessions(w[SESSIONS])} if SESSIONS in w else {})}
    return {"host": host, "sent": float(rec["sent"]), "windows": out}


def load_pushed(now: float | None = None) -> dict:
    """Pushed answers by the pushing machine's host name, those heard from within PUSHED_KEEP_S."""
    now = now or time.time()
    got = _json(pushed_path()).get("machines") or {}
    return {h: m for h, m in got.items() if isinstance(m, dict) and isinstance(m.get("windows"), dict)
            and -REFRESH_S <= now - float(m.get("ts") or 0) <= PUSHED_KEEP_S}


def receive(stream: bytes, via: str, now: float | None = None) -> tuple[dict, int]:
    """`ttp spend-today --receive --via ALIAS`: keep the spend another machine pushed (one JSON record
    on stdin, as push() sends) with this machine's pulled answers. Returns (the ack, the exit code).
    The ack says whether this machine already reads the sender itself (`pulls`), so the sender can
    push less."""
    from .machines import ALIAS_RE
    now = now or time.time()
    if not ALIAS_RE.fullmatch(via or ""):
        return {"error": "--via must be the sending machine's alias"}, 2
    if len(stream) > RECEIVE_BYTES:
        return {"error": "the record is too long; nothing was kept"}, 2
    try:
        got = _valid_push(json.loads(stream), now)
    except (ValueError, RecursionError):       # RecursionError: a deeply nested record
        got = None
    if got is None:
        return {"error": "not a spend record; nothing was kept"}, 2
    fd = _flock("global-spend-pushed.lock", wait=True)
    try:
        doc = _json(pushed_path())
        machines = {h: m for h, m in (doc.get("machines") or {}).items() if isinstance(m, dict)}
        old = machines.get(got["host"]) or {}
        wins = {k: w for k, w in (old.get("windows") or {}).items()
                if isinstance(w, dict) and float(w.get("end") or 0) > now}
        wins.update(got["windows"])
        machines[got["host"]] = {"host": got["host"], "via": via, "ts": now, "sent": got["sent"], "windows": wins}
        keep = sorted(machines, key=lambda h: float(machines[h].get("ts") or 0), reverse=True)[:MAX_PUSHERS]
        project.write_json(pushed_path(), {"machines": {h: machines[h] for h in keep}}, mode=0o600)
    finally:
        os.close(fd)
    pulls = any(m.get("host") == got["host"] and m.get("ok") and 0 <= now - float(m.get("ts") or 0) <= STALE_S
                for m in (load_cache().get("machines") or {}).values() if isinstance(m, dict))
    return {"accepted": len(got["windows"]), "pulls": pulls}, 0


def push_targets() -> list[str]:
    """Where this machine pushes its spend: the account-level `budget.push_spend_to` (a list of ssh
    aliases, or "none"), else every machine the global total asks (targets())."""
    v = (project.load_account_settings().get("budget") or {}).get("push_spend_to")
    if v == "none":
        return []
    if isinstance(v, list):
        return sorted({str(x) for x in v if isinstance(x, str) and NAME_RE.match(x)})
    return targets()


def _push_due(now: float) -> bool:
    st = _json(push_state_path()).get("targets") or {}
    return any(not 0 <= float((st.get(t) or {}).get("tried") or 0) <= now
               or now >= float((st.get(t) or {}).get("next") or 0) for t in push_targets())


def push_windows(budget: dict, now: float) -> list[tuple[float, float]]:
    """The windows a push answers for: this project's, the account-level one and the rolling 24 h,
    so a receiver counts whichever its projects use."""
    acct = project.deep_merge(project.DEFAULT_CONFIG["budget"], project.load_account_settings().get("budget") or {})
    out = []
    for b in (budget, acct, {}):
        start, end, _ = window(b, now)
        if (start, end) not in out:
            out.append((start, end))
    return out[:MAX_WINDOWS]


def push(budget: dict, now: float | None = None) -> int:
    """Push this machine's spend (machine_totals, as `ttp spend-today` gives) to every push target
    that is due, over ssh into `ttp spend-today --receive`. Each target is pushed to every REFRESH_S,
    every PUSH_PULLED_S once it says it reads this machine itself, and with a doubling wait after a
    failure. Only one process of this user pushes at a time; the others skip. Returns how many
    targets took it. Runs in the background (refresh_async), never on the daemon tick."""
    from . import upstream
    now = now or time.time()
    fd = _flock("global-spend-push.lock", wait=False)
    if fd is None:
        return 0
    try:
        st = {t: s for t, s in (_json(push_state_path()).get("targets") or {}).items() if isinstance(s, dict)}
        names = push_targets()
        data, sent = b"", 0
        for t in names:
            s = dict(st.get(t) or {})
            if 0 <= float(s.get("tried") or 0) <= now < float(s.get("next") or 0):
                continue
            if not data:
                wins = [{"start": a, "end": b, **{k: v for k, v in answer(a, b, now=now).items()
                                                  if k in ("rows", "projects", SESSIONS)}} for a, b in push_windows(budget, now)]
                data = (json.dumps({"v": 1, "host": project.hostname(), "sent": now, "windows": wins}) + "\n").encode()
            out, err = upstream.ssh_pipe(t, f"spend-today --receive --via {shlex.quote(upstream.alias())}",
                                         data, PUSH_TIMEOUT_S)
            ack = None
            if out is not None:
                try:
                    ack = json.loads(out.decode(errors="replace").strip().splitlines()[-1])
                except (ValueError, IndexError):
                    pass
                if not isinstance(ack, dict) or not isinstance(ack.get("accepted"), int):
                    ack, err = None, "no ack"
            s["tried"] = now
            if ack is None:
                fails = int(s.get("fails") or 0) + 1
                first, most = PUSH_BACKOFF_S
                s.update(ok=False, fails=fails, error=err[:200], next=now + min(first * 2 ** (fails - 1), most))
            else:
                pulls = ack.get("pulls") is True
                s.update(ok=True, fails=0, error="", last_ok=now, pulls=pulls,
                         next=now + (PUSH_PULLED_S if pulls else REFRESH_S))
                sent += 1
            st[t] = s
            project.write_json(push_state_path(), {"targets": {k: v for k, v in st.items() if k in names}}, mode=0o600)
        return sent
    finally:
        os.close(fd)


def _same_machine(target: str, m: dict, pushed: dict) -> dict | None:
    """The pushed answers of the machine `target` names, if it pushes too: matched by the host name
    its pulled answer gave, its alias, the machines list's or the registry's host name for it."""
    names = {target, m.get("host")}
    try:
        from . import machines
        names.add((machines.load().get(target) or {}).get("hostname"))
    except Exception:
        pass
    names |= {e.get("host") for e in (project.load_registry().get("projects") or {}).values()
              if isinstance(e, dict) and e.get("ssh") == target}
    names.discard(None)
    for h, p in pushed.items():
        if h in names or p.get("via") == target:
            return p
    return None


# the total ----------------------------------------------------------------------------------------
def total(db, provider: str, start: float, end: float, now: float | None = None,
          account: str | None = None, rolling: bool = False) -> dict:
    """The global spend of `provider` on this account in [start, end): this project (`db`), this
    machine's other projects, the other machines (cached or pushed here, stale ones counted) and other
    sources.
    For a budget day a machine's answer counts when it is for the same day, however old. For the
    rolling 24 h (`rolling`) it counts while it was read within STALE_S, even if the last try failed;
    an older one covers another window and is only named stale. A machine is stale when its last try
    failed or its answer for this window is missing or older than STALE_S.
    Each other machine's answer also brings its own other Claude Code sessions (SESSIONS), counted
    with that answer: `remote_sessions_usd` is their sum, `sessions_missing` names the machines whose
    counted answer says nothing of them (an older tt-project) or has them unknown.
    `usd` is the total; `stale` names the machines whose number is stale; `includes` says what it
    counts, in words."""
    now = now or time.time()
    account = account_of(provider) if account is None else account
    own = Path(db.path).resolve()
    conn = db.conn
    usd = sum(r["usd"] for r in _rows(conn, start, end) if _matches(r, provider, account))
    here = machine_totals(start, end, skip=str(own), now=now, cached=True)
    usd += sum(r["usd"] for r in here["rows"] if _matches(r, provider, account))
    stale, errors, hosts, seen = [], list(here["errors"]), [], {project.hostname()}
    remote_projects, remote_sessions, n_sessions, missing = 0, 0.0, 0, []
    cache = load_cache().get("machines") or {}
    pushed = load_pushed(now)
    key = _key(start, end)
    # Each machine once: the ones asked over ssh, then the ones that only push here. A machine both
    # asked and pushing counts with whichever answer for this window is newer.
    found = []
    for t in targets():
        m = cache.get(t) or {}
        w, ok = _windows(m).get(key) or {}, bool(m.get("ok"))
        p = _same_machine(t, m, pushed)
        if p:
            pushed = {h: x for h, x in pushed.items() if x is not p}
            pw = p["windows"].get(key) or {}
            if pw.get("rows") is not None and float(pw.get("ts") or 0) > float(w.get("ts") or 0):
                w, ok = pw, True
        found.append((t, m.get("host") or (p or {}).get("host"), w, ok))
    found += [(p.get("via") or h, h, p["windows"].get(key) or {}, True) for h, p in sorted(pushed.items())]
    for name, host, w, ok in found:
        age = now - float(w.get("ts") or 0)
        if host in seen:
            continue
        if not (ok and age <= STALE_S):
            stale.append(name)
        if w.get("rows") is not None and (age <= STALE_S or not rolling):   # a failed try keeps it
            usd += sum(float(r.get("usd") or 0) for r in w["rows"] if isinstance(r, dict) and _matches(r, provider, account))
            remote_projects += len(w.get("projects") or [])
            o = w.get(SESSIONS)
            if isinstance(o, dict) and _sessions_ok(o):
                if _matches(o, provider, account):
                    remote_sessions += o["usd"]
                    n_sessions += 1
            elif provider == "claude":       # an older tt-project there, or its logs not read yet
                missing.append(name)
        seen.add(host or name)
        hosts.append(name)
    others = []
    for fn in OTHER_SOURCES:
        try:
            more, label = fn(provider, account, start, end)
        except Exception as e:
            errors.append(f"other source: {e}")
            continue
        usd += float(more or 0)
        if label:
            others.append(label)
    n_local = 1 + len(here["projects"])
    includes = (f"{provider} spend on this account by {n_local} tt-project project{'s' if n_local != 1 else ''} "
                f"on this machine")
    if hosts:
        includes += (f" and {remote_projects} on {len(hosts)} other machine{'s' if len(hosts) != 1 else ''}"
                     + (f" ({len(stale)} stale)" if stale else ""))
    includes += "".join(f", {x}" for x in others)
    usd += remote_sessions
    if n_sessions:
        includes += (f", other Claude Code sessions on {n_sessions} other machine{'s' if n_sessions != 1 else ''} "
                     f"({money(remote_sessions)}, estimated)")
    if not others and not n_sessions:
        includes += "; not your own sessions outside tt-project"
    else:
        gap = (f"other Claude Code sessions on {', '.join(missing)} (an older tt-project there, or its logs "
               f"not read yet), " if missing else "")
        includes += f"; not {gap}sessions on machines without tt-project, nor web, desktop or cloud sessions"
    return {"usd": round(usd, 4), "stale": stale, "machines": hosts, "local_projects": n_local,
            "remote_projects": remote_projects, "remote_sessions_usd": round(remote_sessions, 4),
            "sessions_missing": missing, "errors": errors, "includes": includes}


def money(usd: float) -> str:
    """$0.17, $12.50, $340: cents below $100, whole dollars above."""
    return f"${usd:.2f}" if abs(usd) < 100 else f"${usd:.0f}"
