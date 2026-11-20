"""The persistent coordinator's cost per wake, checked without a model call after each session wake.

When the median cost of the last WINDOW ended session wakes (new or resumed, as `ttp stats
--coord-session` counts them) is above coordinator.wake_cost_alarm_usd (default 1.0; 0 or "off": no
check), one alarm goes out for the episode, naming its cause (context tokens, digest size or tool
calls), and the session shrinks itself: an in-place compaction is requested (coordsession "shrink",
once, again only after a whole further window still over) and, while the episode lasts, the digest
sends sections whose only change is an age as unchanged (coordsession "lean"). Never a new or
stateless session. The episode ends, and both clear, once the median is back at or under the
threshold.

Separately (cold_cap), a quiet project whose wake gaps outlive the prompt cache resumes its session cold:
each such wake rewrites the whole context at the cache-write price. When at least COLD_RATE of the last
WINDOW resumed wakes were cold, the session state carries "cold_cap_tokens", the context size whose cold
rewrite costs waste.wake_usd at this project's measured rate, and the default compaction point
(coordsession.limit) stays under it, so the session compacts in place before its cold wakes get dear."""
from __future__ import annotations

import json
import statistics

from . import coordstats

KEY = "coord_wake_cost"   # kv: {"alarmed", "since", "median", "cause", "asked", "after_ask"}
WINDOW = 5
DEFAULT_USD = 1.0
TOOLS_HIGH = 10          # median tool calls at which they lead the cause: each call re-reads the context
DIGEST_SHARE = 0.25      # the digest's share of the context at which it leads the cause
CHARS_PER_TOKEN = 4
COLD_SHARE = 0.5         # a resumed wake whose cache writes are at least this share of its context resumed cold
COLD_RATE = 0.5          # the share of cold resumed wakes at which the compaction point is capped
COLD_MIN_WAKES = 3       # resumed wakes in the window needed to judge the rate


def threshold(cfg: dict) -> float:
    """coordinator.wake_cost_alarm_usd; 0 when off."""
    raw = ((cfg or {}).get("coordinator") or {}).get("wake_cost_alarm_usd", DEFAULT_USD)
    if str(raw).strip().lower() in ("off", "false", "no", "none", ""):
        return 0.0
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return DEFAULT_USD


def threshold_of(value) -> float:
    """The config_set validator of coordinator.wake_cost_alarm_usd."""
    if str(value).strip().lower() in ("off", "false", "no", "none"):
        return 0.0
    try:
        got = float(value)
    except (TypeError, ValueError):
        raise ValueError("coordinator.wake_cost_alarm_usd: give dollars per wake, or 0 for off") from None
    if got < 0:
        raise ValueError("coordinator.wake_cost_alarm_usd: give dollars per wake, or 0 for off")
    return got


def wakes(db, runs_dir, current: dict | None = None, n: int = WINDOW) -> list[dict]:
    """The newest `n` ended session wakes, oldest first: {"id", "usd", "context", "digest", "tools"}.
    `current`: this wake's row as the caller knows it (its cost may not be stored yet)."""
    rows = db.q("SELECT id, started, status, cost_usd, input_tokens, cache_read_tokens, cache_write_tokens, "
                "note, session_id, dir, provider FROM runs WHERE role='coordinator' AND note LIKE '%session%' "
                "AND ended IS NOT NULL ORDER BY started DESC, id DESC LIMIT ?", (n * 4,))
    out = []
    cur = (current or {}).get("id")
    if current:
        out.append(current)
    for r in rows:
        if r["id"] == cur:
            continue
        s = coordstats.session_of(r)
        if not s or s.get("mode") not in ("new", "resume") or r["cost_usd"] is None:
            continue
        out.append(_row(r, s, runs_dir))
        if len(out) >= n:
            break
    return list(reversed(out[:n]))


def _row(r, s: dict, runs_dir) -> dict:
    tools = coordstats.tool_calls(r, runs_dir) or 0
    total = sum(int(r[k] or 0) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
    context = coordstats._first_context(r) or total // (tools + 1)
    return {"id": r["id"], "usd": float(r["cost_usd"] or 0), "context": int(context),
            "digest": int(s.get("sent_chars") or 0) // CHARS_PER_TOKEN, "tools": int(tools),
            "mode": s.get("mode"), "write": int(r["cache_write_tokens"] or 0)}


def current_row(r: dict, usage, session: dict, runs_dir) -> dict:
    """This wake's row from its usage, before the run's cost is stored."""
    row = {k: r.get(k) for k in ("id", "dir", "provider")}
    row.update({"cost_usd": float(getattr(usage, "cost_usd", 0) or 0), "note": json.dumps(
        {"cache_telemetry": (getattr(usage, "extra", None) or {}).get("cache_telemetry") or {}}),
        **{k: getattr(usage, k, 0) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")}})
    return _row(row, session, runs_dir)


def cause(window: list[dict]) -> tuple[str, str]:
    """(cause, numbers) of an expensive window: tool calls, digest size or context tokens."""
    med = {k: int(statistics.median(w[k] for w in window)) for k in ("context", "digest", "tools")}
    nums = (f"median per wake: {med['context']} context tokens, {med['digest']} digest tokens, "
            f"{med['tools']} tool calls")
    if med["tools"] >= TOOLS_HIGH:
        return "tool calls", nums
    if med["context"] and med["digest"] >= DIGEST_SHARE * med["context"]:
        return "digest size", nums
    return "context tokens", nums


def check(cfg: dict, db, runs_dir, session_state: dict, current: dict | None = None) -> tuple[dict, str]:
    """(the coordinator session state with "shrink"/"lean" set or cleared, the alarm text to raise or "")
    after a session wake. Saves its own episode in kv KEY."""
    limit = threshold(cfg)
    st = dict(session_state or {})
    ep = db.kv(KEY) or {}
    window = wakes(db, runs_dir, current) if limit > 0 else []
    if len(window) < WINDOW:
        if limit <= 0 and ep.get("alarmed"):
            st.pop("shrink", None)
            st.pop("lean", None)
            db.set_kv(KEY, {})
        return st, ""
    median = round(statistics.median(w["usd"] for w in window), 2)
    if median <= limit:
        if ep.get("alarmed"):
            st.pop("shrink", None)
            st.pop("lean", None)
        db.set_kv(KEY, {"alarmed": False, "median": median})
        return st, ""
    why, nums = cause(window)
    text = ""
    ep = {**ep, "median": median, "cause": why}
    if not ep.get("alarmed"):
        ep.update({"alarmed": True, "since": window[-1]["id"], "asked": window[-1]["id"], "after_ask": 0})
        st.update({"shrink": True, "lean": True})
        text = (f"Coordinator cost per wake is high: the median of the last {WINDOW} wakes is ${median:.2f}, "
                f"over the ${limit:.2f} alarm. Cause: {why} ({nums}). The session compacts in place on its "
                f"next wake and the digest repeats no section whose only change is an age, until the median "
                f"is back under the alarm.")
    else:
        ep["after_ask"] = int(ep.get("after_ask") or 0) + 1
        st["lean"] = True
        if ep["after_ask"] >= WINDOW:   # a whole window since the last request, still over: ask once more
            ep.update({"asked": window[-1]["id"], "after_ask": 0})
            st["shrink"] = True
    db.set_kv(KEY, ep)
    return st, text


def wake_usd(cfg: dict) -> float:
    """waste.wake_usd: the cost of one wake counted as waste above it; 0 when off."""
    try:
        return max(float(((cfg or {}).get("waste") or {}).get("wake_usd", 1.0) or 0), 0.0)
    except (TypeError, ValueError):
        return 1.0


def cold_rate(window: list[dict]) -> float | None:
    """The share of the window's resumed wakes that resumed cold (cache writes at least COLD_SHARE of
    their context); None with fewer than COLD_MIN_WAKES resumed wakes."""
    resumed = [w for w in window if w.get("mode") == "resume" and w.get("context")]
    if len(resumed) < COLD_MIN_WAKES:
        return None
    return sum(1 for w in resumed if w.get("write", 0) >= COLD_SHARE * w["context"]) / len(resumed)


def cold_cap(cfg: dict, db, runs_dir, session_state: dict, current: dict | None, provider: str,
             model: str = "") -> dict:
    """The session state with "cold_cap_tokens" set to the context size whose cold rewrite costs
    waste.wake_usd, while at least COLD_RATE of the last WINDOW resumed wakes were cold; cleared
    otherwise (a warm project, waste.wake_usd 0, or no price known). coordsession.limit reads it."""
    st = dict(session_state or {})
    usd = wake_usd(cfg)
    rate = cold_rate(wakes(db, runs_dir, current)) if usd > 0 else None
    if rate is None or rate < COLD_RATE:
        st.pop("cold_cap_tokens", None)
        return st
    from .budget import estimate_cost
    per_mtok = estimate_cost(db, cfg, provider, model or "", {"cache_write": 1_000_000})
    if per_mtok <= 0:
        st.pop("cold_cap_tokens", None)
        return st
    st["cold_cap_tokens"] = int(usd / per_mtok * 1_000_000)
    return st
