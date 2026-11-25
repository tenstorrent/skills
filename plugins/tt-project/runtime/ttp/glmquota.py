"""GLM quota pacing (`providers.chat.quota`): spread the chat endpoint's token quota over its window, so
it does not run dry before the reset, and keep judgement runs off it while it is short.

Counting: every chat bridge run adds each answer's tokens (prompt and completion, cached prompt tokens too
unless `count_cached` is false) to one file per endpoint alias, ~/.tt-project/quota/<alias>.json, under a
file lock: every project on this host shares it. The file keeps the window's reset, its tokens used, the
quota's size and per-minute counts for the burn rate. A 429 quota_exceeded answer (chatbridge.quota_until)
syncs it from the body's numbers only (limit, used, resets_at; numeric x-ratelimit headers on that answer
too): used becomes the larger of ours and the server's, as other hosts spend the same quota.

Size: the last 429's limit, else the `size` setting, else what we observed: the most tokens we had counted
in one window when a quota 429 came (the last OBSERVED_KEEP_S); with none of these, nothing is paced
(counting goes on). A quota 429 also marks its window spent, as the server says it is.

Pacing: the window's budget (size less `reserve` of it) spread evenly over the window is the pace line. The
quota is short while the tokens used run more than SLACK of the budget ahead of that line (a burn the even
spread cannot carry to the reset). While short, a new run that would start on the chat provider starts on
the core provider with the GLM fallback model (route fallback `paced`, glmwork.FALLBACK_ON), never a freeze
or rollback (rollout.outage_refusal):
- lowest priority first: each STEP of the budget further ahead sheds one more priority level, 5 first;
- question, plan and review kinds, coordinator runs and specs with a `conclusion:` line, all of them;
- once the whole quota is spent, every run, until the reset.
The allowed rate (quota left over the time to reset) and the burn rate (tokens over the last `rate_window_s`)
are shown beside it. Runs already going keep running: pacing only moves new starts.
Settings (`providers.chat.quota`, every key optional): on (default true; false: no counting, no pacing),
size, window_s (3600), reserve (0.1), rate_window_s (600), count_cached (true).
"""
from __future__ import annotations

import fcntl
import json
import math
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DIR = "quota"
DEFAULTS = {"on": True, "size": 0, "window_s": 3600, "reserve": 0.1, "rate_window_s": 600, "count_cached": True}
BUCKET_S = 60
KEEP_S = 3600                       # per-minute counts kept this long
REASON = "paced"                    # the route entry's fallback while the quota is short (route.REASONS)
SHORT_KINDS = ("question", "plan", "review")
SLACK = 0.02                        # share of the window's budget used ahead of an even spread before pacing acts
STEP = 0.03                         # each further share ahead sheds one more priority level
CONCLUSION = re.compile(r"(?im)^[ \t>*-]*conclusion\s*:")
LIMIT_RE = re.compile(r'"limit"\s*:\s*(\d+)')
USED_RE = re.compile(r'"used"\s*:\s*(\d+)')
RESETS_RE = re.compile(r'"resets_at"\s*:\s*"([^"\s]+)"')
WINDOW_RE = re.compile(r"(?i)\b(minute|hourly|daily)\s+limit\b|\bper\s+(minute|hour|day)\b")
WINDOWS = {"minute": 60, "hourly": 3600, "hour": 3600, "daily": 86400, "day": 86400}
OBSERVED_KEEP_S = 86400             # 429s without a limit: the tokens we had counted, kept this long


def conf(cfg: dict | None) -> dict:
    """The quota settings with defaults; {} when off or no endpoint is set."""
    chat = ((cfg or {}).get("providers") or {}).get("chat") or {}
    raw = chat.get("quota") if isinstance(chat.get("quota"), dict) else {}
    c = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    if c["on"] is False or not chat.get("endpoint"):
        return {}
    return {**c, "key": str(chat["endpoint"])}


def _file(key: str) -> Path:
    from . import project
    return project.HOME_DIR / DIR / (re.sub(r"[^\w.-]", "_", key) + ".json")


@contextmanager
def _locked(key: str):
    path = _file(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path


def _read(path: Path) -> dict:
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) else {}


def _write(path: Path, st: dict) -> None:
    from .project import durable_write
    durable_write(path, json.dumps(st))


def _roll(st: dict, window_s: float, now: float) -> dict:
    """The state in the window holding `now`: a passed reset starts a new window, used 0."""
    window_s = float(window_s or 3600)
    reset = float(st.get("reset") or 0)
    if reset > now:
        return st
    if reset:
        reset += window_s * (math.floor((now - reset) / window_s) + 1)
    else:   # no reset known: windows on the clock's boundaries (the endpoint's are on the hour)
        reset = now - now % window_s + window_s
    return {**st, "reset": reset, "used": 0, "server_used": 0, "spent": 0}


def add(key: str, tokens: int, window_s: float = 3600, now: float | None = None) -> None:
    """Count `tokens` spent on the endpoint `key` now."""
    if not key or tokens <= 0:
        return
    now = time.time() if now is None else now
    with _locked(key) as path:
        st = _roll(_read(path), window_s, now)
        st["used"] = int(st.get("used") or 0) + int(tokens)
        b = {k: v for k, v in (st.get("buckets") or {}).items() if float(k) > now - KEEP_S}
        m = str(int(now - now % BUCKET_S))
        b[m] = int(b.get(m) or 0) + int(tokens)
        st["buckets"], st["ts"] = b, now
        _write(path, st)


def parse(detail: str, headers=None) -> dict:
    """The numbers a quota 429 gives: {limit, used, reset (epoch), window_s}, each only when present."""
    out: dict = {}
    for k, rx in (("limit", LIMIT_RE), ("used", USED_RE)):
        if m := rx.search(detail or ""):
            out[k] = int(m.group(1))
    if m := RESETS_RE.search(detail or ""):
        try:
            out["reset"] = datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    if m := WINDOW_RE.search(detail or ""):
        out["window_s"] = WINDOWS[(m.group(1) or m.group(2)).lower()]
    h = {str(k).lower(): str(v) for k, v in (headers.items() if headers is not None else ())}
    for name, k in (("x-ratelimit-limit-tokens", "limit"), ("x-quota-limit", "limit")):
        if k not in out and (h.get(name) or "").isdigit():
            out[k] = int(h[name])
    for name in ("x-ratelimit-remaining-tokens", "x-quota-remaining"):
        if "used" not in out and "limit" in out and (h.get(name) or "").isdigit():
            out["used"] = max(out["limit"] - int(h[name]), 0)
    return out


def sync(key: str, got: dict, window_s: float = 3600, now: float | None = None, spent: bool = False) -> None:
    """Take a quota 429's numbers (parse): its size, its reset, and used = max(ours, the server's). `spent`
    (a quota_exceeded answer): the window is used up; with no limit given, the tokens we had counted in it
    are an observation of the size."""
    if not key or not (got or spent):
        return
    now = time.time() if now is None else now
    with _locked(key) as path:
        st = _read(path)
        if float(got.get("reset") or 0) > now:
            if float(st.get("reset") or 0) != float(got["reset"]):
                st = {**st, "used": 0, "server_used": 0, "spent": 0}
            st["reset"] = float(got["reset"])
        st = _roll(st, window_s, now)
        ours = int(st.get("used") or 0)
        if int(got.get("limit") or 0) > 0:
            st["size"], st["size_from"] = int(got["limit"]), "429"
        if "used" in got:
            st["server_used"] = int(got["used"])
            st["used"] = max(ours, int(got["used"]))
        if spent:
            if not int(got.get("limit") or 0) and ours > 0:
                st["observed"] = [o for o in (st.get("observed") or []) if float(o[0]) > now - OBSERVED_KEEP_S] + \
                    [[now, ours]]
            st["spent"] = float(st["reset"])   # this window is used up, whatever our count says
        st["last_429"] = {k: got[k] for k in ("limit", "used", "reset", "window_s") if k in got} | {"at": now}
        st["synced"] = st["ts"] = now
        _write(path, st)


def view(cfg: dict | None, now: float | None = None) -> dict:
    """The quota's state now: {} when off; else {key, used, size, size_from, reset, rate, allowed, short,
    ahead (the share of the window's budget used ahead of an even spread), exhausted, paced (size known)}."""
    c = conf(cfg)
    if not c:
        return {}
    now = time.time() if now is None else now
    st = _roll(_read(_file(c["key"])), c["window_s"], now)
    observed = max([int(o[1]) for o in (st.get("observed") or []) if float(o[0]) > now - OBSERVED_KEEP_S] or [0])
    size = int(st.get("size") or 0) or int(c["size"] or 0) or observed
    used = int(st.get("used") or 0)
    span = max(float(c["rate_window_s"] or 600), BUCKET_S)
    rate = sum(int(v) for k, v in (st.get("buckets") or {}).items() if float(k) > now - span) / span
    out = {"key": c["key"], "used": used, "size": size, "reset": float(st["reset"]), "rate": rate,
           "size_from": st.get("size_from") if st.get("size") else "setting" if c["size"] else "observed" if size else "",
           "paced": bool(size), "short": False, "ahead": 0.0, "exhausted": False, "allowed": 0.0,
           "last_429": st.get("last_429") or {}}
    if not size:
        return out
    budget = size * (1 - float(c["reserve"] or 0))
    window_s = float(c["window_s"] or 3600)
    gone = min(max((now - (out["reset"] - window_s)) / window_s, 0.0), 1.0)
    out["allowed"] = max(budget - used, 0) / max(out["reset"] - now, 1)
    out["exhausted"] = used >= size or float(st.get("spent") or 0) == out["reset"]   # the reserve is the margin for runs in flight and the band pacing tracks in
    out["ahead"] = (used - budget * gone) / budget if budget > 0 else 1.0
    out["short"] = out["exhausted"] or out["ahead"] > SLACK
    return out


def shed(task: dict, cls: str | None, v: dict) -> str:
    """Why a run starts on core while the quota is short (view `v`), else ''."""
    if not v.get("short"):
        return ""
    if v.get("exhausted"):
        return "quota left is spent until the reset"
    kind = str(task.get("kind") or "")
    if cls == "coordinator" or kind in SHORT_KINDS:
        return f"a {cls if cls == 'coordinator' else kind} run stays off GLM while its quota is short"
    if CONCLUSION.search(str(task.get("spec") or "")):
        return "a conclusion run stays off GLM while its quota is short"
    try:
        p = min(max(int(task.get("priority") or 3), 1), 5)
    except (TypeError, ValueError):
        p = 3
    levels = math.ceil((float(v.get("ahead") or 0) - SLACK) / STEP)
    return f"priority {p} sheds while the quota is {v['ahead']:.0%} ahead of an even spread" \
        if levels > 0 and p >= 6 - levels else ""


def paced(cfg: dict | None, task: dict, cls: str | None = None, now: float | None = None) -> str:
    """REASON when a run that would start on the chat provider starts on core instead (shed), else ''."""
    try:
        return REASON if shed(task, cls, view(cfg, now)) else ""
    except Exception:   # pacing must never hold a task back
        return ""


def line(cfg: dict | None, now: float | None = None, fmt=None) -> str:
    """One status line: used of size, the reset, and whether pacing acts; '' when off."""
    v = view(cfg, now)
    if not v:
        return ""
    fmt = fmt or (lambda t: time.strftime("%H:%M UTC", time.gmtime(t)))
    used = f"{v['used'] / 1e6:.1f}M"
    hit = v["last_429"]
    last = ("; last quota 429 at " + fmt(hit["at"]) + (f": {hit['used'] / 1e6:.1f}M" if "used" in hit else "") +
            (f" of {hit['limit'] / 1e6:.0f}M" if "limit" in hit else "") + " used") if hit.get("at") else ""
    if not v["size"]:
        return f"GLM quota: {used} tokens used this window (resets {fmt(v['reset'])}); size unknown, pacing waits for it{last}"
    state = ("spent: GLM runs start on core until the reset" if v["exhausted"] else
             f"short: lower-priority and judgement runs start on core ({v['ahead']:.0%} ahead of pace)" if v["short"]
             else "on pace")
    size = f"{v['size'] / 1e6:.0f}M" + (" observed" if v["size_from"] == "observed" else "")
    return f"GLM quota: {used} of {size} tokens used (resets {fmt(v['reset'])}); pacing on, {state}{last}"
