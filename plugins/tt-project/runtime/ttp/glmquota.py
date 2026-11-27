"""GLM quota pacing (`providers.chat.quota`): spread the chat endpoint's token quota over its window, so
it does not run dry before the reset, and keep judgement runs off it while it is short.

Counting: every chat bridge run adds each answer's tokens (prompt and completion, cached prompt tokens too
unless `count_cached` is false) to one file per endpoint alias, ~/.tt-project/quota/<alias>.json, under a
file lock: every project on this host shares it. The file keeps the window's reset, its tokens used and
the quota's size. A 429 quota_exceeded answer (chatbridge.quota_until)
syncs it from the body's numbers only (limit, used, resets_at; numeric x-ratelimit headers on that answer
too): used becomes the larger of ours and the server's, as other hosts spend the same quota.

Size: the last 429's limit, else the `size` setting, else what we observed: the most tokens we had counted
in one window when a quota 429 came (the last OBSERVED_KEEP_S); with none of these, nothing is paced
(counting goes on). A quota 429 also marks its window spent, as the server says it is. A 429 that names its own window
replaces the `window_s` setting until a later 429 names a different one.

Pacing: the window's budget (size less `reserve` of it) spread evenly over the window is the pace line. The
quota is short while the tokens used run more than SLACK of the budget ahead of that line (a burn the even
spread cannot carry to the reset). While short, a new run that would start on the chat provider starts on
the core provider with the GLM fallback model (route fallback `paced`, glmwork.FALLBACK_ON), never a freeze
or rollback (rollout.outage_refusal):
- lowest priority first: each STEP of the budget further ahead sheds one more priority level, 5 first;
- question, plan and review kinds, coordinator runs and specs with a `conclusion:` line, all of them;
- once the whole quota is spent, every run, until the reset.
Runs already going keep running: pacing only moves new starts.
Settings (`providers.chat.quota`, every key optional): on (default true; false: no counting, no pacing),
size, window_s (3600), reserve (0.1), count_cached (true). They are a project's and steer its bridge runs
only; the off-bridge posts (`answer`, `note`) carry no project and always record, cached tokens at full weight.

The org's remaining and our own hourly cap: the quota is the org's (every employee's use shares it), not
ours. Every successful answer on every route (the bridge, and the off-bridge posts through `answer` and
`note`) records its X-Quota-Remaining-llm_tokens header with the time into the same file, and our tokens
(cached ones apart) into the UTC hour they were spent in. The newest reading of the current UTC hour is the
org's remaining. Two limits then shed runs on the same ladder as above, over the UTC hour (`gone`):
- the account setting providers.chat.quota_hourly_cap (tokens an hour; unset or 0: none): our own use this
  UTC hour, cached prompt tokens at full weight, may not pass it;
- the org's remaining less `reserve` of the quota's size: our use may not take more than that.
The tighter budget paces; one spent (our use at the cap, or the org's remaining at the reserve) sends every
run to core until the next UTC hour (a start's route fallback `paced`, never a rollback or freeze).
The cap lifts by itself once cached tokens no longer count (judge): the readings' drops between our own
answers are regressed on our uncached and cached tokens; only when the cached tokens' weight is clearly
small (upper bound under LIFT_MAX_WEIGHT, on at least LIFT_MIN_ROWS readings and LIFT_MIN_CACHED cached
tokens, its standard error under LIFT_MAX_ERR; a noisier set of readings keeps collecting, never votes no;
the fit also takes a charge that shows in the next reading) in LIFT_VOTES evaluations in a row on fresh readings, LIFT_EVERY_S apart, does it lift, once, kept
in the file. A degenerate evaluation never votes and resets the count: a standard error under LIFT_MIN_ERR (a
perfect fit means the readings carry no information), fewer than LIFT_MIN_MOVING readings with a drop, drops
that barely vary (LIFT_MIN_SPREAD), or our own tokens under LIFT_MIN_SHARE of the org's total drop (others
drive the readings, so the fit cannot tell our weight). A quota 429 while lifted, in a UTC hour where our own
use already passed the cap, re-arms the cap (the daemon notifies it once). The account setting providers.chat.quota_cap_lift: auto (default), on (lifted now) or off (never).
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
DEFAULTS = {"on": True, "size": 0, "window_s": 3600, "reserve": 0.1, "count_cached": True}
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
HEADER = "x-quota-remaining-llm_tokens"   # on a successful answer: the org's tokens left this UTC hour
HOUR_S = 3600
HOURS_KEEP_S = 2 * 86400            # our per-UTC-hour use is kept this long
CAP_KEY = "providers.chat.quota_hourly_cap"   # account settings (project.ACCOUNT_NESTED_KEYS)
LIFT_KEY = "providers.chat.quota_cap_lift"
LIFT_MODES = ("auto", "on", "off")
KEYS = (CAP_KEY, LIFT_KEY)
ROW_MIN_S = 5                       # a reading closes a sample row only this long after the last one
ROWS_KEEP = 400
LIFT_EVERY_S = 600                  # one evaluation per this long, each on readings taken since the last
LIFT_VOTES = 3                      # passing evaluations in a row before the cap lifts
LIFT_MIN_ROWS = 40
LIFT_MAX_ERR = 0.2                  # an evaluation votes only once the weight's standard error is under this
LIFT_MIN_CACHED = 10_000_000        # cached tokens of ours inside an evaluation's rows: else it had no power
LIFT_MIN_ERR = 0.01                 # a standard error under this is a degenerate fit (flat or exact readings), no vote
LIFT_MIN_MOVING = 20                # readings with a nonzero drop an evaluation needs
LIFT_MIN_SPREAD = 1000              # tokens: the drops' standard deviation under this is flat readings, no vote
LIFT_MIN_SHARE = 0.3                # our tokens over the org's total drop: under this others drive the readings
LIFT_MAX_WEIGHT = 0.5               # the cached tokens' weight, 2 standard errors up, must be under this


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
    window_s = float(st.get("window_s") or window_s or 3600)   # a 429's own window, when it named one
    reset = float(st.get("reset") or 0)
    if reset > now:
        return st
    if reset:
        reset += window_s * (math.floor((now - reset) / window_s) + 1)
    else:   # no reset known: windows on the clock's boundaries (the endpoint's are on the hour)
        reset = now - now % window_s + window_s
    return {**st, "reset": reset, "used": 0, "spent": 0}


def _tokens(body) -> tuple[int, int]:
    """(tokens to count with cached ones at full weight, cached tokens) of a chat answer's usage object."""
    u = (body or {}).get("usage") if isinstance(body, dict) else None
    if not isinstance(u, dict):
        return 0, 0
    try:
        cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        total = int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0, 0
    return max(total, 0), max(min(cached, total), 0)


def reading(headers) -> int | None:
    """The org's remaining tokens a successful answer's headers give (HEADER), or None."""
    try:
        for k, v in (headers.items() if headers is not None else ()):
            if str(k).lower() == HEADER and str(v).strip().isdigit():
                return int(str(v).strip())
    except Exception:
        pass
    return None


def _record(key: str, window_s: float, now: float, tokens: int = 0, cached: int = 0,
            remaining: int | None = None) -> None:
    with _locked(key) as path:
        st = _read(path)
        if remaining is not None:   # before our tokens, so both routes file them against the same reading
            _reading(st, remaining, now)
        if tokens > 0:
            st = _roll(st, window_s, now)   # a header-only write never rolls: it does not know the window
            st["used"] = int(st.get("used") or 0) + int(tokens)
            hour = str(int(now - now % HOUR_S))
            row = (st.get("hours") or {}).get(hour) or [0, 0]
            st["hours"] = {h: r for h, r in (st.get("hours") or {}).items() if int(h) > now - HOURS_KEEP_S} | \
                {hour: [int(row[0]) + int(tokens), int(row[1]) + int(cached)]}
            pend = st.get("pending") or [0, 0]
            st["pending"] = [int(pend[0]) + int(tokens) - int(cached), int(pend[1]) + int(cached)]
        _write(path, st)


def _reading(st: dict, remaining: int, now: float) -> None:
    """Take the org's remaining `remaining` seen at `now`: the newest reading, and a sample row (the drop since
    the last row's reading in this UTC hour, with our tokens in between) once ROW_MIN_S have passed."""
    hour = now - now % HOUR_S
    st["remaining"] = {"v": int(remaining), "at": now}
    base = st.get("base") or {}
    if float(base.get("at") or 0) < hour:   # the first reading of this UTC hour: the quota refilled
        st["base"], st["pending"] = {"v": int(remaining), "at": now}, [0, 0]
        return
    dt = now - float(base["at"])
    if dt < ROW_MIN_S:
        return
    pend = st.get("pending") or [0, 0]
    rows = (st.get("rows") or [])[-(ROWS_KEEP - 1):]
    rows.append([round(now, 1), round(dt, 1), int(pend[0]), int(pend[1]), int(base["v"]) - int(remaining)])
    st["rows"], st["base"], st["pending"] = rows, {"v": int(remaining), "at": now}, [0, 0]
    _judge(st, now)


def add(key: str, tokens: int, window_s: float = 3600, now: float | None = None, cached: int = 0) -> None:
    """Count `tokens` spent on the endpoint `key` now (`cached` of them cached prompt tokens)."""
    if not key or tokens <= 0:
        return
    _record(key, window_s, time.time() if now is None else now, tokens, cached)


def note(key: str, headers, now: float | None = None) -> int | None:
    """Record the org's remaining from a successful answer's `headers` (HEADER); returns it, None when absent."""
    left = reading(headers)
    if key and left is not None:
        try:
            _record(key, 3600, time.time() if now is None else now, remaining=left)
        except Exception:   # an unwritable or unreadable quota file must not fail the call it rides on
            pass
    return left


def answer(key: str, headers=None, body=None, window_s: float = 3600, now: float | None = None) -> None:
    """One off-bridge GLM answer (a direct post: babysit, benches, probes): its tokens from the body's usage,
    cached ones at full weight, and the header's remaining, in one write. Never raises: counting must not
    break a call."""
    try:
        total, cached = _tokens(body)
        left = reading(headers)
        if key and (total > 0 or left is not None):
            _record(key, window_s, time.time() if now is None else now, total, cached, left)
    except Exception:
        pass


def _fit(rows: list) -> tuple[float, float] | None:
    """Least squares of each row's drop on [seconds, uncached, cached, previous row's uncached, previous row's
    cached] tokens (millions, no intercept): a call's charge may show in its own reading or the next one's.
    The cached tokens' weight (both alignments added) and its standard error; None when the rows do not tell
    the weights apart."""
    n, k = len(rows) - 1, 5
    if n < k + 1:
        return None
    x = [[r[1], r[2] / 1e6, r[3] / 1e6, p[2] / 1e6, p[3] / 1e6] for p, r in zip(rows, rows[1:])]
    y = [r[4] / 1e6 for r in rows[1:]]
    m = [[sum(xi[i] * xi[j] for xi in x) for j in range(k)] + [sum(xi[i] * yi for xi, yi in zip(x, y))] +
         [1.0 if i == j else 0.0 for j in range(k)] for i in range(k)]   # [A | b | I]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-9:
            return None
        m[c], m[piv] = m[piv], m[c]
        m[c] = [v / m[c][c] for v in m[c]]
        for r in range(k):
            if r != c:
                f = m[r][c]
                m[r] = [v - f * w for v, w in zip(m[r], m[c])]
    beta = [m[i][k] for i in range(k)]
    inv = [row[k + 1:] for row in m]
    rss = sum((yi - sum(bj * xj for bj, xj in zip(beta, xi))) ** 2 for xi, yi in zip(x, y))
    var = max(inv[2][2] + inv[4][4] + 2 * inv[2][4], 0.0) * rss / (n - k)
    return beta[2] + beta[4], math.sqrt(var)


def _ours_hour(st: dict, now: float) -> int:
    """Our own tokens in the UTC hour `now` falls in."""
    return int(((st.get("hours") or {}).get(str(int(now - now % HOUR_S))) or [0])[0])


def _flat_or_not_ours(rows: list) -> bool:
    """Whether the rows' drops are too flat, or too little of them ours, for the fit to tell our weight."""
    drops = [r[4] for r in rows]
    mean = sum(drops) / len(drops)
    spread = math.sqrt(sum((d - mean) ** 2 for d in drops) / len(drops))
    total = sum(d for d in drops if d > 0)
    return (sum(d > 0 for d in drops) < LIFT_MIN_MOVING or spread < LIFT_MIN_SPREAD or
            sum(r[2] + r[3] for r in rows) < LIFT_MIN_SHARE * total)


def _judge(st: dict, now: float, mode: str | None = None) -> None:
    """One evaluation of whether cached tokens still count (the module doc), at most every LIFT_EVERY_S and
    only on the rows taken since the last one, so each vote is fresh evidence."""
    lift = st.get("lift") or {}
    if lift.get("lifted") or now - float(lift.get("at") or 0) < LIFT_EVERY_S or (mode or lift_mode()) != "auto":
        return
    rows = [r for r in (st.get("rows") or []) if float(r[0]) > float(lift.get("at") or 0)]
    if len(rows) < LIFT_MIN_ROWS:
        return
    if sum(r[3] for r in rows) < LIFT_MIN_CACHED:   # too little cached traffic to tell: keep collecting
        return
    got = _fit(rows)
    if not got or got[1] > LIFT_MAX_ERR:   # too noisy to tell yet: keep collecting
        return
    if got[1] < LIFT_MIN_ERR or _flat_or_not_ours(rows):   # a degenerate fit tells nothing: no vote, start over
        if lift.get("votes"):
            st["lift"] = {**lift, "votes": 0}
        return
    ok = got[0] + 2 * got[1] < LIFT_MAX_WEIGHT
    votes = int(lift.get("votes") or 0) + 1 if ok else 0
    st["lift"] = {"votes": votes, "at": now, "weight": round(got[0], 3), "err": round(got[1], 3),
                  "lifted": now if votes >= LIFT_VOTES else 0, "told": 0,
                  **{k: lift[k] for k in ("rearmed", "rearm_told") if k in lift}}   # a re-arm's notice still goes out


def lift_mode() -> str:
    """providers.chat.quota_cap_lift from the account settings: auto (default), on or off."""
    from .project import account_value
    v = account_value(LIFT_KEY)
    return v if v in LIFT_MODES else "auto"


def cap_setting() -> int:
    """providers.chat.quota_hourly_cap from the account settings: our tokens an hour at most; 0: none."""
    from .project import account_value
    v = account_value(CAP_KEY)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0


def check_setting(dotted: str, v) -> str:
    """Why `v` is no good for account key `dotted` (CAP_KEY, LIFT_KEY), else ''."""
    if v in ("", None):
        return ""
    if dotted == CAP_KEY and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or not float(v).is_integer()):
        return f"{dotted}: give tokens per UTC hour (a whole number, 0 for no cap)"
    if dotted == LIFT_KEY and v not in LIFT_MODES:
        return f"{dotted}: give one of {', '.join(LIFT_MODES)}"
    return ""


def _tell_once(key: str, flag: str, told: str, now: float | None) -> dict:
    """The lift record when its `flag` is set and not yet told (marking it told now), else {}: each daemon
    notice goes out once per host."""
    now = time.time() if now is None else now
    with _locked(key) as path:
        st = _read(path)
        lift = st.get("lift") or {}
        if not lift.get(flag) or lift.get(told):
            return {}
        st["lift"] = {**lift, told: now}
        _write(path, st)
    return lift


def lift_notice(key: str, now: float | None = None) -> str:
    """Once, the text saying the self-lift happened (the daemon notifies it); '' otherwise."""
    if not (lift := _tell_once(key, "lifted", "told", now)):
        return ""
    return (f"GLM hourly cap lifted by itself: {lift.get('votes')} evaluations in a row found cached tokens no "
            f"longer count against the org quota (weight {lift.get('weight')} +- {lift.get('err')}). "
            f"Force it back with `ttp config --account {LIFT_KEY} off`.")


def rearm_notice(key: str, now: float | None = None) -> str:
    """Once, the text saying a quota 429 re-armed the lifted cap (the daemon notifies it); '' otherwise."""
    if not _tell_once(key, "rearmed", "rearm_told", now):
        return ""
    return ("GLM hourly cap re-armed: the org quota refused us while the cap was lifted and our own use was past "
            f"the cap. The lift is cleared; it is judged again from fresh readings. Keep it off with "
            f"`ttp config --account {LIFT_KEY} off`.")


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
        if got.get("window_s"):
            st["window_s"] = float(got["window_s"])   # the 429 names its own window (minute, hourly)
        if float(got.get("reset") or 0) > now:
            if float(st.get("reset") or 0) != float(got["reset"]):
                st = {**st, "used": 0, "spent": 0}
            st["reset"] = float(got["reset"])
        st = _roll(st, window_s, now)
        ours = int(st.get("used") or 0)
        if int(got.get("limit") or 0) > 0:
            st["size"], st["size_from"] = int(got["limit"]), "429"
        if "used" in got:
            st["used"] = max(ours, int(got["used"]))
        if spent:
            if lift_mode() == "auto" and (st.get("lift") or {}).get("lifted") and 0 < cap_setting() <= \
                    _ours_hour(st, now):
                st["lift"] = {"votes": 0, "at": now, "lifted": 0, "rearmed": now, "told": 0}   # the org refused us
            if not int(got.get("limit") or 0) and ours > 0:
                st["observed"] = [o for o in (st.get("observed") or []) if float(o[0]) > now - OBSERVED_KEEP_S] + \
                    [[now, ours]]
            st["spent"] = float(st["reset"])   # this window is used up, whatever our count says
        st["last_429"] = {k: got[k] for k in ("limit", "used") if k in got} | {"at": now}
        _write(path, st)


def lifted(st: dict) -> bool:
    """Whether the hourly cap is lifted: forced on, or the self-lift found cached tokens no longer count."""
    mode = lift_mode()
    return mode == "on" or (mode == "auto" and bool((st.get("lift") or {}).get("lifted")))


def view(cfg: dict | None, now: float | None = None) -> dict:
    """The quota's state now: {} when off; else {key, used, size, size_from, reset, short, ahead (the share
    of the budget used ahead of an even spread), exhausted, last_429, and the hourly view: cap (our setting,
    0 none or lifted), ours_hour (our tokens this UTC hour, cached ones in full), remaining (the org's, newest
    reading of this UTC hour, else None), why}. Each limit (the window's size, our cap, the org's remaining)
    gives its own ahead and exhausted; the stricter wins."""
    c = conf(cfg)
    if not c:
        return {}
    now = time.time() if now is None else now
    st = _roll(_read(_file(c["key"])), c["window_s"], now)
    observed = max([int(o[1]) for o in (st.get("observed") or []) if float(o[0]) > now - OBSERVED_KEEP_S] or [0])
    size = int(st.get("size") or 0) or int(c["size"] or 0) or observed
    used = int(st.get("used") or 0)
    out = {"key": c["key"], "used": used, "size": size, "reset": float(st["reset"]),
           "size_from": st.get("size_from") if st.get("size") else "setting" if c["size"] else "observed" if size else "",
           "short": False, "ahead": 0.0, "exhausted": False, "why": "",
           "last_429": st.get("last_429") or {}, "cap": 0, "ours_hour": 0, "remaining": None, "lifted": lifted(st)}
    reserve = float(c["reserve"] or 0)
    if size:
        budget = size * (1 - reserve)
        window_s = float(st.get("window_s") or c["window_s"] or 3600)
        gone = min(max((now - (out["reset"] - window_s)) / window_s, 0.0), 1.0)
        out["exhausted"] = used >= size or float(st.get("spent") or 0) == out["reset"]   # the reserve is the margin for runs in flight and the band pacing tracks in
        out["ahead"] = (used - budget * gone) / budget if budget > 0 else 1.0
        if out["exhausted"]:
            out["why"] = "quota left is spent until the reset"
    hour = now - now % HOUR_S
    gone = (now - hour) / HOUR_S
    ours = _ours_hour(st, now)
    left = st.get("remaining") or {}
    out["ours_hour"] = ours
    if float(left.get("at") or 0) >= hour:
        out["remaining"] = int(left["v"])
    cap = 0 if out["lifted"] else cap_setting()
    out["cap"] = cap
    # (budget this UTC hour, why it is spent) per limit: our cap less its reserve; ours plus what the org
    # has left over its reserve
    limits = []
    if cap:
        limits.append((cap * (1 - reserve), f"our {cap / 1e6:g}M tokens an hour are spent until the next UTC hour"))
    if out["remaining"] is not None:
        margin = reserve * size
        limits.append((ours + max(out["remaining"] - margin, 0),
                       f"the org's quota has {out['remaining'] / 1e6:.1f}M tokens left this UTC hour"))
    for budget, why in limits:
        ahead = (ours - budget * gone) / budget if budget > 0 else 1.0
        if ours >= budget:
            out["exhausted"], out["why"] = True, out["why"] or why
        if ahead > out["ahead"]:
            out["ahead"] = ahead
    out["short"] = out["exhausted"] or out["ahead"] > SLACK
    return out


def shed(task: dict, cls: str | None, v: dict) -> str:
    """Why a run starts on core while the quota is short (view `v`), else ''."""
    if not v.get("short"):
        return ""
    if v.get("exhausted"):
        return v.get("why") or "quota left is spent until the reset"
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
    hourly = ""
    if v["cap"]:
        hourly += f"; our cap {v['cap'] / 1e6:g}M an hour ({v['ours_hour'] / 1e6:.1f}M used this UTC hour)"
    elif v["lifted"]:
        hourly += "; our hourly cap is lifted"
    if v["remaining"] is not None:
        hourly += f"; org has {v['remaining'] / 1e6:.1f}M left this UTC hour"
    last += hourly
    if not v["size"]:
        return f"GLM quota: {used} tokens used this window (resets {fmt(v['reset'])}); size unknown, pacing waits for it{last}"
    state = ("spent: GLM runs start on core until the reset" if v["exhausted"] else
             f"short: lower-priority and judgement runs start on core ({v['ahead']:.0%} ahead of pace)" if v["short"]
             else "on pace")
    size = f"{v['size'] / 1e6:.0f}M" + (" observed" if v["size_from"] == "observed" else "")
    return f"GLM quota: {used} of {size} tokens used (resets {fmt(v['reset'])}); pacing on, {state}{last}"
