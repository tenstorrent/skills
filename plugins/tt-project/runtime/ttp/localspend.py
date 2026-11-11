# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Claude Code spend on this machine that tt-project did not start, estimated from the local session
logs, for the account's global daily total (globalcap.py).

Claude Code writes every session to <CLAUDE_CONFIG_DIR or ~/.claude>/projects/<folder>/<id>.jsonl,
and a session's subagents to <folder>/<id>/subagents/*.jsonl. Each assistant record carries the API
call's usage; one call is written once per content block, so a call counts once per (message id,
request id), and the '<synthetic>' model (no API call) not at all. Calls are priced at list prices
(PRICES, published per model; account-level `pricing.claude` overrides), scaled per model by the
factor finished runs measured against Claude Code's reported cost (calibrate), and booked by their time
into the budget day (budget.day_start in budget.timezone; without a day_start, by the hour).

Every local tt-project project's runs are left out by their session id (each project's database,
opened read-only), whatever their provider: a provider that drives Claude Code against another
model's endpoint writes the same logs, and its spend is booked (or not billed here) by its own
provider. The claude provider gives each run its id before it starts, so a run still going is never
counted as other spend. Calls to a model that is not a Claude model are never Anthropic spend and
are skipped. Nothing else is left out: sessions that workers start themselves are real spend no
ledger books.

Whether the estimate counts toward the global dollar cap depends on how this machine's Claude Code
login is billed (billing_mode, read from its login state, never from a machine name): an API key or
a usage-based seat is billed per use and counts; a subscription plan login is metered by its plan
windows (5h, 7d), never in dollars, and is only shown; a login that cannot be told is shown as
estimated, not billed. A key exported only in an interactive shell is seen through the login
state's approved-key record, not the daemon's environment; beside a plan login it cannot be told.

The scan is incremental: the daemon lists the folders every SCAN_S, skips files untouched since
before the days it keeps, and reads each file only from where it stopped, up to the last complete
line. What it found lives in ~/.tt-project/session-spend.json (under a lock beside it), per day and
per session, with the calls already counted, so a file rewritten or truncated is read again without
counting a call twice.

Codex sessions are read the same way from <CODEX_HOME or ~/.codex>/sessions rollouts (the Codex
section below) into their own cache, priced at the Codex table (account-level `pricing.codex` over
it), always as an estimate, and left out by the same session ids (a run still going by the thread id
on its output's first line). A Codex API-key login is billed per use; a ChatGPT login is a plan.
Cursor keeps no local usage log: its other sessions are named unknown.

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
    "claude-haiku-5-5": (0.1, 0.5, 0.01, 0.125, 0.2),   # prompts up to 100K tokens; longer ones bill 5x
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25, 2.0),
    "claude-3-5-haiku": (0.8, 4.0, 0.08, 1.0, 1.6),
}
WEB_SEARCH_USD = 0.01        # per search ($10 per 1,000)
FAST_X = 2.0                 # fast mode (usage.speed 'fast') doubles every rate
US_GEO_X = 1.1               # US-only inference (usage.inference_geo 'us')
SYNTHETIC = "<synthetic>"    # written by Claude Code itself, no API call

SCAN_S = 300                 # the daemon scans this often
SLACK_S = 3600               # files untouched this long before the kept days are skipped
PLAN_WORDS = ("max", "pro", "team", "enterprise", "subscription")   # a plan login's subscription words
KEEP_S = 2 * gcap.DAY        # calls are kept (and deduplicated) this far back from the current day
MEMO_S = 30                  # one process reads the estimate again after this long
DRIFT = 0.01                 # a measured drift up to this is noise: the table's prices stand
DRIFT_RUNS = 3               # a model's factor needs at least this many runs
CONSISTENT = 0.05            # ... each within this share of their median factor
DOMINANT = 0.9               # a run calibrates the model that priced at least this share of it
ALERT_DRIFT = 0.20           # a drift past this is told (low, once per model and price-table row)
FACTOR_MIN, FACTOR_MAX = 0.5, 2.0   # a factor outside these is never applied (told once)
ALERTED_KEEP = 50
CALIBRATION_KEEP = 10
CALIBRATION_WAIT_S = 2.0     # calibrate() waits at most this long for the cache lock
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


def row_key(model: str, rows: dict) -> str | None:
    """The id of `model`'s row (a date or '[1m]' suffix and a cloud prefix ignored), or None."""
    m = (model or "").lower()
    m = m[m.find("claude-"):] if "claude-" in m else m
    m = m.split("@", 1)[0]
    for k in sorted(rows, key=len, reverse=True):
        if m.startswith(k) and _SUFFIX.match(m[len(k):]):
            return k
    return None


def row_for(model: str, rows: dict) -> tuple[tuple, bool]:
    """(rates, estimated) of `model`: its row; a model not in the table gets the most expensive row
    and is estimated."""
    k = row_key(model, rows)
    if k is not None:
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


def _json_file(path: Path) -> dict:
    try:
        got = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def billing_mode(env=None) -> str:
    """How this machine's Claude Code login is billed, from its login state: 'usage' (an API key, or
    a usage-based seat: every call billed per use), 'plan' (a subscription plan login: metered by
    its plan windows, not in dollars) or 'unknown'. Read from <CLAUDE_CONFIG_DIR>/.claude.json (else
    ~/.claude.json), the config folder's settings.json and .credentials.json; never a secret's value.

    Not only the calling process's environment (the daemon's, or an ssh-run spend-today's) tells of
    an API key: a key exported only in an interactive shell shows as an approved key in the login
    state (customApiKeyResponses.approved, which Claude Code fills when the user accepts an
    ANTHROPIC_API_KEY). With no subscription login beside it that is usage billing; beside a plan
    login it is 'unknown', since the key overrides the plan only while it is still exported. The
    session logs carry no sign of how a call was paid, so they are not read for this."""
    env = os.environ if env is None else env
    folder = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else None
    state = _json_file(folder / ".claude.json" if folder else Path.home() / ".claude.json")
    folder = folder or Path.home() / ".claude"
    settings = _json_file(folder / "settings.json")
    senv = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    if (env.get("ANTHROPIC_API_KEY") or senv.get("ANTHROPIC_API_KEY") or settings.get("apiKeyHelper")
            or state.get("primaryApiKey")):
        return "usage"
    acct = state.get("oauthAccount") if isinstance(state.get("oauthAccount"), dict) else {}
    oauth = _json_file(folder / ".credentials.json").get("claudeAiOauth")
    oauth = oauth if isinstance(oauth, dict) else {}
    if "usage" in str(acct.get("seatTier") or "").lower():
        return "usage"
    asked = state.get("customApiKeyResponses") if isinstance(state.get("customApiKeyResponses"), dict) else {}
    if isinstance(asked.get("approved"), list) and any(isinstance(k, str) and k for k in asked["approved"]):
        return "unknown" if acct or oauth else "usage"
    words = " ".join(str(x) for x in (oauth.get("subscriptionType"), acct.get("organizationType"),
                                      acct.get("billingType")) if x).lower()
    return "plan" if (acct or oauth) and any(w in words for w in PLAN_WORDS) else "unknown"


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
        if not isinstance(usage, dict) or model == SYNTHETIC or "claude" not in model.lower() or ts is None:
            continue   # no API call ('<synthetic>'), or another vendor's model: not Anthropic spend
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


# Codex ------------------------------------------------------------------------------------------
# Codex writes each session to <CODEX_HOME or ~/.codex>/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl.
# Its event_msg token_count events carry the session's running total_token_usage (input with the
# cached part inside it, and output); a call is what a total grew by since the one before.
ROLLOUT_ID = re.compile(r"rollout-.*?([0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27})\.jsonl$")
CODEX_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")


def codex_root() -> Path:
    from .providers.codex import codex_home
    return codex_home() / "sessions"


def codex_files(root: Path, since: float) -> list[tuple[str, os.stat_result]]:
    """Rollouts under `root` changed at or after `since`. No folder yet is no sessions; one that
    cannot be listed raises."""
    if not os.path.lexists(root):
        return []
    os.listdir(root)
    out = []
    for path in root.glob("*/*/*/rollout-*.jsonl"):
        try:
            st = path.stat(follow_symlinks=False)
        except OSError:
            continue
        if st.st_mtime >= since and path.is_file():
            out.append((str(path), st))
    return out


def codex_calls(lines, rec: dict, totals: dict) -> "list[dict]":
    """The token use in a rollout's `lines` (bytes): {ts, key, session, model, usage}, one per
    token_count event whose total grew past the session's last one (`totals`, per session id,
    updated). `rec` keeps the file's session id and current model (turn_context) across reads. A
    total that shrank starts over and counts whole; the key is the session and its total, so a
    reading seen again (a file read again, a resumed session's copy) counts once."""
    out = []
    for line in lines:
        if b'"token_count"' not in line and b'"turn_context"' not in line and b'"session_meta"' not in line:
            continue
        try:
            ev = json.loads(line)
            p = ev["payload"]
            kind = p.get("type") if ev.get("type") == "event_msg" else ev.get("type")
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if kind == "session_meta":
            rec["sid"] = str(p.get("id") or rec.get("sid") or "")
        elif kind == "turn_context":
            rec["model"] = str(p.get("model") or rec.get("model") or "")
        elif kind == "token_count" and isinstance(p.get("info"), dict):
            total = p["info"].get("total_token_usage")
            ts, sid = _ts(ev.get("timestamp")), str(rec.get("sid") or "")
            if not isinstance(total, dict) or ts is None:
                continue
            try:
                tok = [max(int(total.get(k) or 0), 0) for k in CODEX_KEYS]
            except (TypeError, ValueError):
                continue
            was = totals.get(sid) or [0, 0, 0]
            if any(a < b for a, b in zip(tok, was)):
                was = [0, 0, 0]
            totals[sid] = tok
            grew = [a - b for a, b in zip(tok, was)]
            if any(grew):
                out.append({"ts": ts, "key": hashlib.sha1(f"codex\0{sid}\0{tok}".encode()).hexdigest()[:16],
                            "session": sid, "model": str(rec.get("model") or ""), "usage": grew})
    return out


def codex_price(model: str, grew: list[int], rows: dict) -> tuple[float, bool]:
    """List price in $ of `grew` (input, cached, output tokens) at `rows` (the Codex table with the
    account's `pricing.codex`), and whether the model had no row of its own (priced at 'default')."""
    from .providers.base import price_row
    pin, pcached, pout = price_row(rows, {}, model)
    inp, cached, out = grew
    return (max(inp - cached, 0) * pin + cached * pcached + out * pout) / 1e6, model not in rows


def codex_rows() -> dict:
    from .providers.codex import PRICES as CODEX_PRICES
    own = ((project.load_account_settings().get("pricing") or {}).get("codex")) or {}
    return {**CODEX_PRICES, **{k: v for k, v in own.items() if isinstance(v, (list, tuple)) and len(v) == 3}}


def codex_billing() -> str:
    """How this machine's Codex login is billed, from CODEX_HOME's auth.json: 'usage' (an API key),
    'plan' (a ChatGPT login, metered by its plan windows) or 'unknown' (no login, another kind)."""
    from .providers.codex import _auth, _mode
    auth = _auth()
    how = _mode(auth).lower().replace("_", "") if auth else ""
    return {"apikey": "usage", "chatgpt": "plan"}.get(how, "unknown")


# the day ----------------------------------------------------------------------------------------
def mode(budget: dict) -> str:
    """What a bucket is: the fixed budget day, or (no day_start) the UTC hour."""
    return f"{budget.get('day_start') or ''}|{gcap.zone(budget)[1]}" if budget.get("day_start") else "hour"


def bucket(budget: dict, ts: float) -> float:
    day = gcap.day_bounds(budget, ts)
    return day[0] if day else ts - ts % 3600


# the cache --------------------------------------------------------------------------------------
KINDS = ("claude", "codex")   # whose local logs are read, each into its own cache


def cache_path(kind: str = "claude") -> Path:
    return project.HOME_DIR / ("session-spend.json" if kind == "claude" else f"{kind}-session-spend.json")


def load(kind: str = "claude") -> dict:
    try:
        data = json.loads(cache_path(kind).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def scan(budget: dict, now: float | None = None, root: Path | None = None, kind: str = "claude") -> dict:
    """Read what the logs of `kind` (Claude Code's, or Codex's rollouts) added since the last scan
    into its cache, and return it. One process at a time (a lock beside the cache); a scan already
    running elsewhere returns the cache as it is. A corrupt cache, other day settings or another logs
    folder start over from scratch."""
    now = now or time.time()
    codex = kind == "codex"
    root = Path(root or (codex_root() if codex else logs_root()))
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    with open(project.HOME_DIR / f"{'' if kind == 'claude' else kind + '-'}session-spend.lock", "a") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return load(kind)
        cache = load(kind)
        fresh = {"v": 2, "mode": mode(budget), "root": str(root), "files": {}, "buckets": {}}
        if not (cache.get("v") == 2 and cache.get("mode") == fresh["mode"] and cache.get("root") == str(root)
                and isinstance(cache.get("files"), dict) and isinstance(cache.get("buckets"), dict)):
            cache = {**fresh, "calibration": cache.get("calibration") or []}
        keep_from = bucket(budget, now) - KEEP_S
        buckets = {k: b for k, b in cache["buckets"].items() if float(k) >= keep_from}
        seen = {key for b in buckets.values() for key in b.get("k") or []}
        try:
            found = (codex_files if codex else log_files)(root, keep_from - SLACK_S)
        except OSError as e:
            cache.update(ok=False, error=f"cannot read the session logs: {e.strerror or e}", scanned=now)
            project.write_json(cache_path(kind), cache, mode=0o600)
            return cache
        files = {}
        rows = codex_rows() if codex else table(overrides())
        scale, keys = ({}, {}) if codex else (factors(rows[0]), {})
        totals = cache.get("totals") if isinstance(cache.get("totals"), dict) else {}
        for path, st in found:
            was = cache["files"].get(path) or {}
            off = int(was.get("off") or 0)
            if was.get("ino") != st.st_ino or st.st_size < off:
                off = 0   # replaced or truncated: read again; counted calls stay counted once
            rec = {}
            if codex:   # a rollout's id from its name until its session_meta names it
                m = ROLLOUT_ID.search(os.path.basename(path))
                rec = ({"sid": was["sid"], "model": was.get("model") or ""} if off and was.get("sid")
                       else {"sid": m.group(1) if m else path, "model": ""})
            if st.st_size > off and not (was.get("ino") == st.st_ino and was.get("size") == st.st_size):
                try:
                    lines, off = _read_from(path, off)
                except OSError:
                    continue
                got = codex_calls(lines, rec, totals) if codex else calls(lines)
                for c in got:
                    if c["ts"] < keep_from or c["key"] in seen:
                        continue
                    seen.add(c["key"])
                    if codex:
                        usd, est = codex_price(c["model"], c["usage"], rows)
                    else:
                        usd, est = price(c["model"], c["usage"], rows=rows)
                        if c["model"] not in keys:
                            keys[c["model"]] = row_key(c["model"], rows[0])
                        usd *= scale.get(keys[c["model"]], 1.0)
                    b = buckets.setdefault(f"{bucket(budget, c['ts']):.0f}", {"s": {}, "k": [], "unknown": []})
                    b["k"].append(c["key"])
                    s = b["s"].setdefault(c["session"], [0.0, 0])
                    s[0] = round(s[0] + usd, 6)
                    if est:
                        s[1] = 1
                        if c["model"] not in b["unknown"]:
                            b["unknown"].append(c["model"])
            files[path] = {"ino": st.st_ino, "size": st.st_size, "off": off, **rec}
        cache.update(files=files, buckets=buckets, ok=True, error="", scanned=now)
        if codex:
            live = {f.get("sid") for f in files.values()}
            cache["totals"] = {k: v for k, v in totals.items() if k in live}
        project.write_json(cache_path(kind), cache, mode=0o600)
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
    due = [k for k, c in ((k, load(k)) for k in KINDS)
           if not (c.get("mode") == mode(budget) and now - float(c.get("scanned") or 0) < SCAN_S)]
    if not due:
        return
    t = threading.Thread(target=lambda: _scan_quietly(dict(budget), now, due), daemon=True)
    _THREAD["t"] = t
    t.start()


def _scan_quietly(budget: dict, now: float, kinds=("claude",)) -> None:
    """scan() of each of `kinds` in the background. A failure leaves its cache as it was; the next due
    scan tries again."""
    for kind in kinds:
        try:
            scan(budget, now, kind=kind)
        except Exception:
            pass


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
                elif b'"thread.started"' in line:   # codex exec --json: a run going has no id recorded yet
                    ev = json.loads(line)
                    if ev.get("type") == "thread.started":
                        return str(ev.get("thread_id") or "")
    except (OSError, ValueError):
        pass
    return ""


def tt_sessions(since: float) -> tuple[set[str], list[str]]:
    """Session ids of every local tt-project project's runs, of any provider, going or ended since
    `since` (each database opened read-only), and the projects that could not be read."""
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
                    f"SELECT id, {sel}, note FROM runs WHERE ended IS NULL OR ended>=?",
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


def estimate(budget: dict, start: float, end: float, now: float | None = None,
             kind: str = "claude") -> dict | None:
    """Other Claude Code (or Codex, `kind`) spend on this machine in [start, end): {usd, sessions,
    estimated, unknown, billing (billing_mode or codex_billing)}, or None when it is unknown (no scan
    yet, or the logs could not be read)."""
    now = now or time.time()
    key = (str(project.HOME_DIR), start, end, mode(budget), kind)
    hit = _MEMO.get(key)
    if hit and now - hit[0] < MEMO_S:
        return hit[1]
    c = load(kind)
    out = None
    if c.get("ok") and c.get("v") == 2 and c.get("mode") == mode(budget) and now - float(c.get("scanned") or 0) < 4 * SCAN_S:
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
        out = {"usd": round(usd, 4), "sessions": len(n), "estimated": est, "unknown": sorted(unknown),
               "billing": codex_billing() if kind == "codex" else billing_mode()}
    _MEMO[key] = (now, out)
    return out


def source(provider: str, account: str, start: float, end: float) -> tuple[float, str, str]:
    """globalcap's other-spend source: this machine's other Claude Code or Codex sessions, with how
    they are billed (globalcap counts only 'usage' toward the cap). Cursor keeps no local log of its
    use: its other sessions are named unknown, never $0."""
    if provider == "codex":
        e = estimate(account_budget(), start, end, kind="codex")
        if e is None:
            return 0.0, "other Codex sessions on this machine (unknown: the local rollouts are not read yet)", "usage"
        flag = ", some at the default row" if e["unknown"] else ""
        return e["usd"], (f"other Codex sessions on this machine ({gcap.money(e['usd'])}, estimated from local "
                          f"rollouts at list prices{flag})"), e.get("billing") or codex_billing()
    if provider == "cursor":
        return 0.0, "other Cursor sessions on this machine (unknown: Cursor keeps no local usage log)", "usage"
    if provider != "claude":
        return 0.0, "", "usage"
    e = estimate(account_budget(), start, end)
    if e is None:
        return 0.0, "other Claude Code sessions on this machine (unknown: the local logs are not read yet)", "usage"
    flag = ", some at a guessed price" if e["estimated"] else ""
    return e["usd"], (f"other Claude Code sessions on this machine ({gcap.money(e['usd'])}, estimated from local "
                      f"logs at list prices{flag})"), e.get("billing") or billing_mode()


gcap.add_other_source(source)


def field(start: float, end: float, now: float | None = None) -> dict | None:
    """What `ttp spend-today` tells other machines about this machine's other Claude Code sessions in
    [start, end) (globalcap.answer): the estimate with this machine's account key, so a machine on
    another account leaves it out, and how this machine's login is billed, or None while it is
    unknown."""
    e = estimate(account_budget(), start, end, now)
    if e is None:
        return None
    return {"provider": "claude", "key": gcap.account_key("claude", gcap.account_of("claude")),
            "usd": e["usd"], "sessions": e["sessions"], "estimated": True, "billing": e.get("billing") or billing_mode()}


# calibration ------------------------------------------------------------------------------------
def _session_priced(session_id: str, root: Path | None = None) -> tuple[float, dict, set] | None:
    """The list price of one session's log (its subagents too), its price per table row, and the
    models the table has no row for; None when there is no log."""
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
    rows, seen, usd, by_key, unknown = table(overrides()), set(), 0.0, {}, set()
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
            usd += u
            k = row_key(c["model"], rows[0])
            if k is None:
                unknown.add(c["model"])
            else:
                by_key[k] = by_key.get(k, 0.0) + u
    return usd, by_key, unknown


def session_cost(session_id: str, root: Path | None = None) -> tuple[float, bool] | None:
    """The list price of one session's log (its subagents too), or None when there is no log."""
    got = _session_priced(session_id, root)
    return None if got is None else (got[0], bool(got[2]))


def calibration_path() -> Path:
    return project.HOME_DIR / "price-calibration.json"


def load_calibration() -> dict:
    try:
        data = json.loads(calibration_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def factors(rows: dict) -> dict[str, float]:
    """The applied factor per table row: one measured against the row's current rates, so a release or
    a pricing.claude override that changes a row drops its factor until runs measure it again."""
    out, models = {}, load_calibration().get("models")
    for k, m in (models.items() if isinstance(models, dict) else ()):
        try:   # a hand-edited or torn file applies nothing, never breaks a scan
            f = float(m["factor"])
            same = m.get("applied") is True and k in rows and list(m["rates"]) == list(rows[k])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if same and FACTOR_MIN <= f <= FACTOR_MAX:
            out[k] = f
    return out


def calibrate(session_id: str, reported_usd: float, now: float | None = None,
              root: Path | None = None) -> dict | None:
    """Price a finished run's session log, compare it with the cost Claude Code reported, and keep
    the last CALIBRATION_KEEP pairs. A run is a pair of the model that priced at least DOMINANT of it,
    measured against that model's current rates. Once DRIFT_RUNS of a model's pairs agree within
    CONSISTENT of their median factor (reported / priced), that factor is recorded in
    price-calibration.json and applied to scan()'s estimate of other sessions, when it lies within
    FACTOR_MIN..FACTOR_MAX and the drift (priced / reported - 1) is past DRIFT. It only moves the
    estimate toward the cost Claude Code measured, never past it, and it is bounded, so a bad
    measurement can at most halve or double the estimate the global daily cap counts.
    Returns {model, drift, factor, samples, applied, release, unpriced, alert, key}, with `alert` the
    one thing worth telling this time ('unpriced', 'bound' or 'drift' past ALERT_DRIFT), or None: a run
    too cheap to compare, no log, or a cache lock still busy (a scan) after CALIBRATION_WAIT_S. Each is
    told at most once across this machine's projects per `key`: what a release must change to end it
    (the model's rates for 'drift' and 'bound', the model's name for 'unpriced'), never the release
    itself, which moves many times a day."""
    if reported_usd < 0.05:
        return None
    got = _session_priced(session_id, root)
    if not got:
        return None
    usd, by_key, unknown = got
    rows = table(overrides())[0]
    model = max(by_key, key=by_key.get) if by_key else ""
    pair = (None if unknown or not model or by_key[model] < DOMINANT * usd
            else [now or time.time(), round(reported_usd, 6), round(usd, 6), model, list(rows[model])])
    from . import __version__ as release
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    with open(project.HOME_DIR / "session-spend.lock", "a") as lk:
        give_up = time.monotonic() + CALIBRATION_WAIT_S
        while True:   # a long scan holds the lock: skip this run rather than stall the daemon
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= give_up:
                    return None
                time.sleep(0.05)
        cache = load()
        pairs = [x for x in (cache.get("calibration") or []) if isinstance(x, list) and len(x) == 5]
        if pair:
            pairs = (pairs + [pair])[-CALIBRATION_KEEP:]
            cache["calibration"] = pairs
            project.write_json(cache_path(), cache, mode=0o600)
        state = load_calibration()
        models = state.get("models") if isinstance(state.get("models"), dict) else {}
        alerted = [str(a) for a in state.get("alerted") or []] if isinstance(state.get("alerted"), list) else []
        out = {"model": model, "drift": None, "factor": None, "samples": 0, "applied": False,
               "release": release, "unpriced": sorted(unknown), "alert": None, "key": None}
        mine = [x for x in pairs if pair and x[3] == model and x[4] == pair[4] and x[2] > 0]
        fs = [r / p for _, r, p, _, _ in mine]
        med = statistics.median(fs) if fs else 0.0
        if med > 0 and sum(abs(f / med - 1) <= CONSISTENT for f in fs) >= DRIFT_RUNS:
            drift = 1 / med - 1
            within = FACTOR_MIN <= med <= FACTOR_MAX
            out.update(drift=drift, factor=round(med, 4), samples=len(fs), applied=within and abs(drift) > DRIFT)
            models[model] = {"factor": out["factor"], "samples": len(fs), "drift": round(drift, 4),
                             "applied": out["applied"], "rates": pair[4], "release": release,
                             "ts": now or time.time()}
            kind = "drift" if out["applied"] and abs(drift) > ALERT_DRIFT else "" if within else "bound"
        else:
            kind = ""
        if unknown:   # only a table that lists the model ends it
            kind, tags = "unpriced", [f"unpriced:{m}" for m in sorted(unknown)]
        elif not kind:
            tags = []
        else:    # only new rates for the model end it
            rates = hashlib.sha256(json.dumps(pair[4]).encode()).hexdigest()[:10]
            tags = [f"{kind}:{model}:{rates}"]
        new = [t for t in tags if t not in alerted]
        if new:
            out.update(alert=kind, key=f"unpriced:{', '.join(sorted(unknown))}" if unknown else tags[0])
            alerted = (alerted + new)[-ALERTED_KEEP:]
        if out["alert"] or out["samples"]:
            project.write_json(calibration_path(), {"v": 1, "models": models, "alerted": alerted}, mode=0o600)
    return out


def alert_text(got: dict | None) -> str:
    """What a calibrate() result with an alert tells the user: what the project did, never a chore."""
    kind = (got or {}).get("alert")
    if kind == "unpriced":
        return (f"Claude Code used {', '.join(got['unpriced'])}, which tt-project's price table does not list. "
                f"tt-project prices it at the table's most expensive row, an over-estimate on the safe side of the "
                f"global daily cap, and leaves it out of calibration. A tt-project release that lists it ends this.")
    if kind not in ("drift", "bound"):
        return ""
    off = (f"The price table prices {got['model']} {abs(got['drift']) * 100:.0f}% "
           f"{'above' if got['drift'] > 0 else 'below'} what Claude Code reported over {got['samples']} recent runs.")
    if kind == "drift":
        return (f"{off} tt-project now scales its estimate of this machine's other Claude Code sessions on that "
                f"model by {got['factor']:.2f} to match, so the global daily cap counts the measured cost.")
    return (f"{off} That is outside the {FACTOR_MIN}x-{FACTOR_MAX}x range tt-project corrects by itself, so it "
            f"keeps the table's prices for that model and its estimate of other sessions may be off by that much. "
            f"A tt-project release with an updated table ends this.")
