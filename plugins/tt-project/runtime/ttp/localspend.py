# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Claude Code spend on this machine that tt-project did not start, estimated from the local session
logs, for the account's global daily total (globalcap.py).

Claude Code writes every session to <CLAUDE_CONFIG_DIR or ~/.claude>/projects/<folder>/<id>.jsonl,
and a session's subagents to <folder>/<id>/subagents/*.jsonl. Each assistant record carries the API
call's usage; one call is written once per content block, so a call counts once per (message id,
request id), and the '<synthetic>' model (no API call) not at all. Calls are priced at list prices
(PRICES, published per model; account-level `pricing.claude` overrides) and booked by their time
into the budget day (budget.day_start in budget.timezone; without a day_start, by the hour).

Every local tt-project project's runs are left out by their session id (each project's database,
opened read-only): the claude provider gives each run its id before it starts, so a run still going
is never counted as other spend. Nothing else is left out: sessions that workers start themselves
are real spend no ledger books.

The scan is incremental: the daemon lists the folders every SCAN_S, skips files untouched since
before the days it keeps, and reads each file only from where it stopped, up to the last complete
line. What it found lives in ~/.tt-project/session-spend.json (under a lock beside it), per day and
per session, with the calls already counted, so a file rewritten or truncated is read again without
counting a call twice.

What it cannot see: other machines (each machine reports only its own logs), claude.ai web, desktop
and mobile, cloud sessions, sessions run with --no-session-persistence or another
CLAUDE_CONFIG_DIR, API calls missing from the log (about 1%), and discounts off the list price.
"""
from __future__ import annotations

import calendar
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import statistics
import threading
import time
from pathlib import Path

from . import globalcap as gcap
from . import project

# $ per million tokens: (input, output, cache read, 5-minute cache write, 1-hour cache write), from the
# published list (platform.claude.com/docs/en/about-claude/pricing). Keys are model ids without a
# date suffix.
PRICES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-mythos-5-1": (10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5, 20.0),
    "claude-mythos-5": (10.0, 50.0, 1.0, 12.5, 20.0),
    "claude-opus-5-5": (4.0, 20.0, 0.2, 5.0, 8.0),
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25, 10.0),
    "claude-opus-4-8": (5.0, 25.0, 0.5, 6.25, 10.0),
    "claude-opus-4-7": (5.0, 25.0, 0.5, 6.25, 10.0),
    "claude-opus-4-6": (5.0, 25.0, 0.5, 6.25, 10.0),
    "claude-opus-4-5": (5.0, 25.0, 0.5, 6.25, 10.0),
    "claude-opus-4-1": (15.0, 75.0, 1.5, 18.75, 30.0),
    "claude-opus-4": (15.0, 75.0, 1.5, 18.75, 30.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.2, 2.5, 4.0),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5, 4.0),
    "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75, 6.0),
    "claude-sonnet-4-5": (3.0, 15.0, 0.3, 3.75, 6.0),
    "claude-sonnet-4": (3.0, 15.0, 0.3, 3.75, 6.0),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25, 2.0),
    "claude-3-5-haiku": (0.8, 4.0, 0.08, 1.0, 1.6),
}
WEB_SEARCH_USD = 0.01        # per search ($10 per 1,000)
FAST_X = 2.0                 # fast mode (usage.speed 'fast') doubles every rate
US_GEO_X = 1.1               # US-only inference (usage.inference_geo 'us')
SYNTHETIC = "<synthetic>"    # written by Claude Code itself, no API call

SCAN_S = 300                 # the daemon scans this often
SLACK_S = 3600               # files untouched this long before the kept days are skipped
KEEP_S = 2 * gcap.DAY        # calls are kept (and deduplicated) this far back from the current day
MEMO_S = 30                  # one process reads the estimate again after this long
DRIFT = 0.01                 # the price table is stale once it is this far off Claude Code's own cost
DRIFT_RUNS = 3               # over at least this many runs
CALIBRATION_KEEP = 10
_SUFFIX = re.compile(r"^(-\d{8})?(\[[^\]]*\])?$")


# prices -----------------------------------------------------------------------------------------
def table(overrides: dict | None = None) -> tuple[dict, float]:
    """The price rows with `overrides` (model: [5 rates]) on top, and the web search fee."""
    rows = dict(PRICES)
    fee = WEB_SEARCH_USD
    for k, v in (overrides or {}).items():
        if k == "web_search" and isinstance(v, (int, float)):
            fee = float(v)
        elif isinstance(v, (list, tuple)) and len(v) == 5:
            try:
                rows[str(k)] = tuple(float(x) for x in v)
            except (TypeError, ValueError):
                pass
    return rows, fee


def row_for(model: str, rows: dict) -> tuple[tuple, bool]:
    """(rates, estimated) of `model`: the row whose id it is (a date or '[1m]' suffix and a cloud
    prefix ignored); a model not in the table gets the most expensive row and is estimated."""
    m = (model or "").lower()
    m = m[m.find("claude-"):] if "claude-" in m else m
    m = m.split("@", 1)[0]
    for k in sorted(rows, key=len, reverse=True):
        if m.startswith(k) and _SUFFIX.match(m[len(k):]):
            return rows[k], False
    return max(rows.values(), key=lambda r: (r[1], r[0])), True


def price(model: str, usage: dict, overrides: dict | None = None, rows=None) -> tuple[float, bool]:
    """List price in $ of one API call's `usage`, and whether it is estimated (unknown model)."""
    rows, fee = rows or table(overrides)
    r, est = row_for(model, rows)
    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else None
    if cc is not None:
        w5, w1 = int(cc.get("ephemeral_5m_input_tokens") or 0), int(cc.get("ephemeral_1h_input_tokens") or 0)
    else:   # no split recorded: the 5-minute write, the API's default
        w5, w1 = int(usage.get("cache_creation_input_tokens") or 0), 0
    usd = (int(usage.get("input_tokens") or 0) * r[0] + int(usage.get("output_tokens") or 0) * r[1]
           + int(usage.get("cache_read_input_tokens") or 0) * r[2] + w5 * r[3] + w1 * r[4]) / 1e6
    if usage.get("speed") == "fast":
        usd *= FAST_X
    if usage.get("inference_geo") == "us":
        usd *= US_GEO_X
    stu = usage.get("server_tool_use") if isinstance(usage.get("server_tool_use"), dict) else {}
    usd += int(stu.get("web_search_requests") or 0) * fee
    return usd, est


def overrides() -> dict:
    return ((project.load_account_settings().get("pricing") or {}).get("claude")) or {}


# the logs ---------------------------------------------------------------------------------------
def logs_root() -> Path:
    from .providers.claude import claude_config_dir
    return claude_config_dir() / "projects"


def log_files(root: Path, since: float) -> list[tuple[str, os.stat_result]]:
    """Session and subagent logs under `root` changed at or after `since`. Raises if `root` cannot
    be listed."""
    out = []
    with os.scandir(root) as top:
        folders = [e.path for e in top if e.is_dir(follow_symlinks=False)]
    for folder in folders:
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_file(follow_symlinks=False) and e.name.endswith(".jsonl"):
                    st = e.stat(follow_symlinks=False)
                    if st.st_mtime >= since:
                        out.append((e.path, st))
                elif e.is_dir(follow_symlinks=False):
                    sub = os.path.join(e.path, "subagents")
                    with os.scandir(sub) as it2:
                        for s in it2:
                            if s.name.endswith(".jsonl") and s.is_file(follow_symlinks=False):
                                st = s.stat(follow_symlinks=False)
                                if st.st_mtime >= since:
                                    out.append((s.path, st))
            except OSError:
                continue
    return out


def _ts(s: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return None


def calls(lines) -> "list[dict]":
    """The assistant records with usage in `lines` (bytes): {ts, key, session, model, usage}."""
    out = []
    for line in lines:
        if b'"assistant"' not in line or b'"usage"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        usage = msg.get("usage")
        model = str(msg.get("model") or "")
        ts = _ts(rec.get("timestamp"))
        if not isinstance(usage, dict) or model == SYNTHETIC or ts is None:
            continue
        ident = f"{msg.get('id') or ''}\0{rec.get('requestId') or ''}"
        if ident == "\0":
            ident = str(rec.get("uuid") or line)
        out.append({"ts": ts, "key": hashlib.sha1(ident.encode()).hexdigest()[:16],
                    "session": str(rec.get("sessionId") or ""), "model": model, "usage": usage})
    return out


def _read_from(path: str, offset: int) -> tuple[list[bytes], int]:
    """Complete lines from `offset` on, and the offset after the last one (a partial last line
    waits for the next scan)."""
    lines = []
    with open(path, "rb") as f:
        f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):
                break
            lines.append(line)
            offset += len(line)
    return lines, offset


# the day ----------------------------------------------------------------------------------------
def mode(budget: dict) -> str:
    """What a bucket is: the fixed budget day, or (no day_start) the UTC hour."""
    return f"{budget.get('day_start') or ''}|{gcap.zone(budget)[1]}" if budget.get("day_start") else "hour"


def bucket(budget: dict, ts: float) -> float:
    day = gcap.day_bounds(budget, ts)
    return day[0] if day else ts - ts % 3600


# the cache --------------------------------------------------------------------------------------
def cache_path() -> Path:
    return project.HOME_DIR / "session-spend.json"


def load() -> dict:
    try:
        data = json.loads(cache_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def scan(budget: dict, now: float | None = None, root: Path | None = None) -> dict:
    """Read what the logs added since the last scan into the cache, and return it. One process at a
    time (a lock beside the cache); a scan already running elsewhere returns the cache as it is. A
    corrupt cache, other day settings or another logs folder start over from scratch."""
    now = now or time.time()
    root = Path(root or logs_root())
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    with open(project.HOME_DIR / "session-spend.lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return load()
        cache = load()
        fresh = {"v": 1, "mode": mode(budget), "root": str(root), "files": {}, "buckets": {}}
        if not (cache.get("v") == 1 and cache.get("mode") == fresh["mode"] and cache.get("root") == str(root)
                and isinstance(cache.get("files"), dict) and isinstance(cache.get("buckets"), dict)):
            cache = {**fresh, "calibration": cache.get("calibration") or []}
        keep_from = bucket(budget, now) - KEEP_S
        buckets = {k: b for k, b in cache["buckets"].items() if float(k) >= keep_from}
        seen = {key for b in buckets.values() for key in b.get("k") or []}
        try:
            found = log_files(root, keep_from - SLACK_S)
        except OSError as e:
            cache.update(ok=False, error=f"cannot read the session logs: {e.strerror or e}", scanned=now)
            project.write_json(cache_path(), cache, mode=0o600)
            return cache
        files = {}
        rows = table(overrides())
        for path, st in found:
            was = cache["files"].get(path) or {}
            off = int(was.get("off") or 0)
            if was.get("ino") != st.st_ino or st.st_size < off:
                off = 0   # replaced or truncated: read again; counted calls stay counted once
            if st.st_size > off and not (was.get("ino") == st.st_ino and was.get("size") == st.st_size):
                try:
                    lines, off = _read_from(path, off)
                except OSError:
                    continue
                for c in calls(lines):
                    if c["ts"] < keep_from or c["key"] in seen:
                        continue
                    seen.add(c["key"])
                    usd, est = price(c["model"], c["usage"], rows=rows)
                    b = buckets.setdefault(f"{bucket(budget, c['ts']):.0f}", {"s": {}, "k": [], "unknown": []})
                    b["k"].append(c["key"])
                    s = b["s"].setdefault(c["session"], [0.0, 0])
                    s[0] = round(s[0] + usd, 6)
                    if est:
                        s[1] = 1
                        if c["model"] not in b["unknown"]:
                            b["unknown"].append(c["model"])
            files[path] = {"ino": st.st_ino, "size": st.st_size, "off": off}
        cache.update(files=files, buckets=buckets, ok=True, error="", scanned=now)
        project.write_json(cache_path(), cache, mode=0o600)
        return cache


_THREAD: dict[str, threading.Thread] = {}


def account_budget() -> dict:
    """The budget settings every project on this machine shares: the estimate is the machine's, so
    its days follow the account-level day, never one project's own."""
    return project.layered({}).get("budget") or {}


def scan_async(budget: dict, now: float | None = None) -> None:
    """Start a scan in the background when this project's global cap is on (`budget`), one is due and
    none is running."""
    now = now or time.time()
    if float(budget.get("global_daily_usd") or 0) <= 0:
        return
    budget = account_budget()
    t = _THREAD.get("t")
    if t and t.is_alive():
        return
    c = load()
    if c.get("mode") == mode(budget) and now - float(c.get("scanned") or 0) < SCAN_S:
        return
    t = threading.Thread(target=lambda: gcap._quiet(scan, dict(budget), now), daemon=True)
    _THREAD["t"] = t
    t.start()


# tt-project's own runs --------------------------------------------------------------------------
def _init_session(output: Path) -> str:
    """The session id in a run's init event (runs from before runs.session_id existed)."""
    try:
        with open(output, "rb") as f:
            for _ in range(50):
                line = f.readline()
                if not line:
                    break
                if b'"init"' in line:
                    ev = json.loads(line)
                    if ev.get("type") == "system" and ev.get("subtype") == "init":
                        return str(ev.get("session_id") or "")
    except (OSError, ValueError):
        pass
    return ""


def tt_sessions(since: float) -> tuple[set[str], list[str]]:
    """Session ids of every local tt-project project's claude runs going or ended since `since`
    (each database opened read-only), and the projects that could not be read."""
    ids, errors = set(), []
    for name, db in gcap.local_projects():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as e:
            errors.append(f"{name}: {e}")
            continue
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            sel = "session_id" if "session_id" in cols else "NULL"
            for rid, sid, note in conn.execute(
                    f"SELECT id, {sel}, note FROM runs WHERE provider='claude' AND (ended IS NULL OR ended>=?)",
                    (since,)):
                try:
                    noted = (json.loads(note or "{}") or {}).get("session_id")
                except ValueError:
                    noted = None
                got = {x for x in (sid, noted) if x}
                if not got:
                    got.add(_init_session(db.parent / "runs" / str(rid) / "output.jsonl"))
                ids |= {x for x in got if x}
        except sqlite3.Error as e:
            errors.append(f"{name}: {e}")
        finally:
            conn.close()
    return ids, errors


# the estimate -----------------------------------------------------------------------------------
_MEMO: dict[tuple, tuple[float, dict | None]] = {}


def estimate(budget: dict, start: float, end: float, now: float | None = None) -> dict | None:
    """Other Claude Code spend on this machine in [start, end): {usd, sessions, estimated, unknown},
    or None when it is unknown (no scan yet, or the logs could not be read)."""
    now = now or time.time()
    key = (str(project.HOME_DIR), start, end, mode(budget))
    hit = _MEMO.get(key)
    if hit and now - hit[0] < MEMO_S:
        return hit[1]
    c = load()
    out = None
    if c.get("ok") and c.get("mode") == mode(budget) and now - float(c.get("scanned") or 0) < 4 * SCAN_S:
        mine, _ = tt_sessions(start - SLACK_S)
        lo = bucket(budget, start)
        usd, n, est, unknown = 0.0, set(), False, set()
        for k, b in (c.get("buckets") or {}).items():
            if not lo <= float(k) < end:
                continue
            for sid, (amount, flag) in (b.get("s") or {}).items():
                if sid in mine:
                    continue
                usd += amount
                n.add(sid)
                est = est or bool(flag)
            unknown |= set(b.get("unknown") or [])
        out = {"usd": round(usd, 4), "sessions": len(n), "estimated": est, "unknown": sorted(unknown)}
    _MEMO[key] = (now, out)
    return out


def source(provider: str, account: str, start: float, end: float) -> tuple[float, str]:
    """globalcap's other-spend source: this machine's other Claude Code sessions."""
    if provider != "claude":
        return 0.0, ""
    e = estimate(account_budget(), start, end)
    if e is None:
        return 0.0, "other Claude Code sessions on this machine (unknown: the local logs are not read yet)"
    flag = ", some at a guessed price" if e["estimated"] else ""
    return e["usd"], (f"other Claude Code sessions on this machine ({gcap.money(e['usd'])}, estimated from local "
                      f"logs at list prices{flag})")


gcap.add_other_source(source)


# calibration ------------------------------------------------------------------------------------
def session_cost(session_id: str, root: Path | None = None) -> tuple[float, bool] | None:
    """The list price of one session's log (its subagents too), or None when there is no log."""
    root = Path(root or logs_root())
    if not session_id or "/" in session_id:
        return None
    paths = []
    try:
        with os.scandir(root) as top:
            for e in top:
                if e.is_dir(follow_symlinks=False):
                    f = os.path.join(e.path, f"{session_id}.jsonl")
                    if os.path.isfile(f):
                        paths.append(f)
                        sub = os.path.join(e.path, session_id, "subagents")
                        if os.path.isdir(sub):
                            paths += [os.path.join(sub, n) for n in os.listdir(sub) if n.endswith(".jsonl")]
    except OSError:
        return None
    if not paths:
        return None
    rows, seen, usd, est = table(overrides()), set(), 0.0, False
    for p in paths:
        try:
            lines, _ = _read_from(p, 0)
        except OSError:
            continue
        for c in calls(lines):
            if c["key"] in seen:
                continue
            seen.add(c["key"])
            u, e = price(c["model"], c["usage"], rows=rows)
            usd, est = usd + u, est or e
    return usd, est


def calibrate(session_id: str, reported_usd: float, now: float | None = None,
              root: Path | None = None) -> float | None:
    """Price a finished run's session log, compare it with the cost Claude Code reported, and keep
    the last CALIBRATION_KEEP pairs. Returns the median drift (priced / reported - 1) once there are
    DRIFT_RUNS of them, else None. Runs with an unknown model, or too cheap to compare, are skipped."""
    if reported_usd < 0.05:
        return None
    got = session_cost(session_id, root)
    if not got or got[1]:
        return None
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    with open(project.HOME_DIR / "session-spend.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cache = load()
        pairs = [x for x in (cache.get("calibration") or []) if isinstance(x, list) and len(x) == 3]
        pairs = (pairs + [[now or time.time(), round(reported_usd, 6), round(got[0], 6)]])[-CALIBRATION_KEEP:]
        cache["calibration"] = pairs
        project.write_json(cache_path(), cache, mode=0o600)
    if len(pairs) < DRIFT_RUNS:
        return None
    return statistics.median(p / r - 1 for _, r, p in pairs)
