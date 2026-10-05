# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The account's day and the global daily cap: what every tt-project project billed to one account
has spent today, on every machine tt-project can see.

Once `budget.day_start` ("HH:MM") is set, the day is fixed: it starts at that time in
`budget.timezone` (an IANA zone, default UTC; never the host's own zone, since servers run on UTC)
and lasts until the same wall-clock time the next day, so it is 23 or 25 hours long across a
daylight-saving change. The default, an empty `day_start`, keeps the rolling 24 hours. Weekly caps
stay rolling 7 days. `budget.global_daily_usd` defaults to 0 (off).

The global total for a provider counts, for this day:
- this machine: every project in the registry (`ttp list`) whose host is this machine, read from its
  database read-only, plus running work as last priced;
- other machines: the hosts of the registry's projects on other machines, and machines-list entries
  tagged `tt-project`, reached by ssh (BatchMode, as `ttp` already reaches them) running
  `ttp spend-today` there, which sums only that machine's own projects. Each machine's answer is
  cached in ~/.tt-project/global-spend.json with when it was read. A machine that cannot be reached
  keeps its last answer for the same day, which still counts and is shown as stale;
- other spend sources registered with `add_other_source` (a hook for spend outside tt-project, e.g.
  the user's own sessions; none ship yet).

Only spend on the same provider and the same account counts: a Codex project, or a project logged
in to another account, is not billed with this one. Rows with no account recorded count (fail safe).

The settings live in the account-level file ~/.tt-project/settings.json (`ttp config --account KEY
VALUE`), which every project on the machine reads; a project's own project.json may override them.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import sqlite3
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import project
from . import db as dbmod

DAY = 86400.0
REFRESH_S = 600            # each other machine is asked again this long after the last try
STALE_S = 1800             # an answer older than this, or a failed last try, is stale
LOCAL_CACHE_S = 30         # this machine's other projects are read again after this long
TAG = "tt-project"         # machines-list tag of a machine that runs tt-project projects
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
REMOTE_TTP = "~/.tt-project/lib/current/bin/ttp"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]

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
    """Spend per (provider, account key) in [start, end), with running work as last priced."""
    out: dict[tuple, float] = {}
    where, args = dbmod.counted_spend(start, end)     # the same rule as the project caps
    for prov, acct, usd in conn.execute(
            f"SELECT provider, COALESCE(account,''), SUM(usd) FROM ledger WHERE {where} "
            "GROUP BY provider, account", args):
        k = (prov, account_key(prov or "", acct))
        out[k] = out.get(k, 0.0) + float(usd or 0)
    for prov, acct, usd in conn.execute(
            "SELECT provider, COALESCE(account,''), SUM(cost_usd) FROM runs WHERE status='running' "
            "GROUP BY provider, account"):
        k = (prov, account_key(prov or "", acct))
        out[k] = out.get(k, 0.0) + float(usd or 0)
    return [{"provider": p, "key": k, "usd": round(u, 4)} for (p, k), u in out.items()]


def _read_only(path: Path, start: float, end: float) -> list[dict]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
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
    _LOCAL[key] = (now, start, end, out)
    return out


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


def fetch(target: str, start: float, end: float) -> dict:
    """Ask another machine for its projects' spend in [start, end). Raises on any failure."""
    cmd = f"{REMOTE_TTP} spend-today --since {start:.0f} --until {end:.0f} --json"
    r = subprocess.run([*SSH, target, cmd], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(((r.stderr or "").strip().splitlines() or [f"exit {r.returncode}"])[-1][:200])
    data = json.loads(r.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise RuntimeError("unexpected answer")
    return data


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
            if not force and now - float(m.get("tried") or 0) < REFRESH_S and m.get("start") == start:
                continue
            try:
                got = fetch(t, start, end)
            except Exception as e:
                machines[t] = {**m, "tried": now, "ok": False, "error": str(e)[:200]}
                continue
            machines[t] = {"tried": now, "ts": now, "ok": True, "host": got.get("host") or t, "start": start,
                           "end": end, "rows": got["rows"], "projects": got.get("projects") or []}
        for gone in set(machines) - set(targets()):
            del machines[gone]
        project.write_json(cache_path(), cache, mode=0o600)
        return cache


_THREAD: dict[str, threading.Thread] = {}


def refresh_async(budget: dict, now: float | None = None) -> None:
    """Start a refresh in the background when one is due and none is running: ssh may take seconds."""
    now = now or time.time()
    if float(budget.get("global_daily_usd") or 0) <= 0:
        return
    t = _THREAD.get("t")
    if t and t.is_alive():
        return
    start, end, _ = window(budget, now)
    cache = (load_cache().get("machines") or {})
    if not any(now - float((cache.get(x) or {}).get("tried") or 0) >= REFRESH_S
               or (cache.get(x) or {}).get("start") != start for x in targets()):
        return
    t = threading.Thread(target=lambda: _quiet(refresh, start, end), daemon=True)
    _THREAD["t"] = t
    t.start()


def _quiet(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:
        pass


# the total ----------------------------------------------------------------------------------------
def total(db, provider: str, start: float, end: float, now: float | None = None,
          account: str | None = None, rolling: bool = False) -> dict:
    """The global spend of `provider` on this account in [start, end): this project (`db`), this
    machine's other projects, the other machines (cached, stale ones counted) and other sources.
    For a budget day a machine's answer counts when it is for the same day, however old. For the
    rolling 24 h (`rolling`) it counts while it is fresh (read within STALE_S); an older one covers
    another window and is only named stale.
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
    remote_projects = 0
    cache = load_cache().get("machines") or {}
    for t in targets():
        m = cache.get(t) or {}
        fresh = bool(m.get("ok")) and now - float(m.get("ts") or 0) <= STALE_S
        if m.get("host") in seen:
            continue
        if not fresh:
            stale.append(t)
        same = (fresh if rolling else
                m.get("start") is not None and abs(float(m["start"]) - start) < 60)
        if m.get("rows") is not None and same:
            usd += sum(float(r.get("usd") or 0) for r in m["rows"] if isinstance(r, dict) and _matches(r, provider, account))
            remote_projects += len(m.get("projects") or [])
        seen.add(m.get("host") or t)
        hosts.append(t)
    others = []
    for fn in OTHER_SOURCES:
        try:
            more, label = fn(provider, account, start, end)
        except Exception as e:
            errors.append(f"other source: {e}")
            continue
        usd += float(more or 0)
        others.append(label)
    n_local = 1 + len(here["projects"])
    includes = (f"{provider} spend on this account by {n_local} tt-project project{'s' if n_local != 1 else ''} "
                f"on this machine")
    if hosts:
        includes += (f" and {remote_projects} on {len(hosts)} other machine{'s' if len(hosts) != 1 else ''}"
                     + (f" ({len(stale)} stale)" if stale else ""))
    includes += "".join(f", {x}" for x in others)
    includes += "; not your own sessions outside tt-project" if not others else ""
    return {"usd": round(usd, 4), "stale": stale, "machines": hosts, "local_projects": n_local,
            "remote_projects": remote_projects, "errors": errors, "includes": includes}


def money(usd: float) -> str:
    """$0.17, $12.50, $340: cents below $100, whole dollars above."""
    return f"${usd:.2f}" if abs(usd) < 100 else f"${usd:.0f}"
