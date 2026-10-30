"""Prompt cache lifetime of each coordinator turn when coordinator.cache_ttl is "auto".

A 1h cache write costs 2x input and a 5m one 1.25x; a read costs the same either way, and a turn
sent at 5m still reads a prefix an earlier turn wrote at 1h. So a turn that follows a 1h turn with
the same prefix (system prompt and stable context, action schema, model, effort, agent CLI) soon
enough to find its entries alive goes at 5m. Any other (cold) turn goes at 1h when a next turn looks
likely within the hour (likely_next), so the many gaps of 5-60 min between turns keep hitting; in a
quiet project, where none follows, it goes at 5m. A 5m turn never extends the window: only a 1h turn's
start counts.

The 1h turns are kept per prefix in the kv table, so the choice survives a daemon restart, and a
turn whose prefix alternates (a raised effort, say) finds its own 1h entry again."""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import time

AUTO = "auto"
KEY = "coord_cache_1h"   # kv: prefix key -> {"ts": start of its last 1h turn, the parts it was keyed on}
WARM_MIN = 50            # coordinator.cache_warm_min: a 1h turn younger than this is warm
KEEP = 8                 # prefixes remembered
PARTS = ("prompt", "schema", "model", "effort", "cli")
# A cold turn's 1h write pays only if a turn reads it within the hour. likely_next guesses that
# model-free; with no sign of one the turn goes at 5m.
NEXT_MIN = 60            # a task or llm schedule due this soon starts work whose end wakes the coordinator
CHAT_MIN = 30            # a user message this recent: the chat is open
GAPS = 5                 # the recent turn gaps whose median is weighed
QUIET_GAP_MIN = 90       # a median gap over this, with nothing running or due, is a quiet project
# Accepted gap: some 1h writes on the due and median-gap reasons go unread. A task stays "due" when it
# is overdue but held, or asleep on a probe that keeps saying "not yet"; a burst of turns a minute apart
# shortens the median. A replay of live turns found every narrower rule tried (dropping a task overdue
# over NEXT_MIN, a task asleep on a probe, or gaps under 5 min from the median) adding about one 5m miss
# per unread write it saved, and a miss costs more (a cold rewrite) than an unread 1h write (0.75x input).
# So they stay as they are: overall the due reasons were read about 88% of the time, the median one 73%.


WORKER_TTLS = ("5m", "1h", "off")
WORKER_DEFAULT = "5m"


def worker_setting(budget: dict) -> str:
    """budget.worker_cache_ttl, the prompt cache lifetime of every run but the coordinator's: "5m" (the
    default: a write at 1.25x input instead of 2x, and worker calls rarely sit 5 min apart), "1h", or
    "off" (no setting: the agent's own default). Unset, "" or an unknown value is the default."""
    v = str((budget or {}).get("worker_cache_ttl") or "").strip().lower()
    return v if v in WORKER_TTLS else WORKER_DEFAULT


def worker_problems(budget: dict) -> list[str]:
    v = (budget or {}).get("worker_cache_ttl")
    if v is None or str(v).strip().lower() in WORKER_TTLS + ("",):
        return []
    return [f"budget.worker_cache_ttl: {v!r} is not one of {', '.join(WORKER_TTLS)}; {WORKER_DEFAULT} is used"]


def setting(c: dict) -> str:
    """coordinator.cache_ttl as set, lower-cased; unset, null or "" (left by an older template) is "auto"."""
    return str(c.get("cache_ttl") or "").strip().lower() or AUTO


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def cli_stamp(binary: str | None) -> str:
    """Fingerprint of the agent CLI: where its binary resolves to, its mtime and size. An update
    (a new version swapped in or relinked) changes it; "" when there is no binary."""
    if not binary:
        return ""
    try:
        real = os.path.realpath(binary)
        st = os.stat(real)
    except OSError:
        return ""
    return _h(f"{real}:{st.st_mtime_ns}:{st.st_size}")


def fingerprint(system: str | None, context: str | None, schema: dict | None, model: str, effort: str,
                cli: str) -> dict:
    return {"prompt": _h(f"{system or ''}\0{context or ''}"),
            "schema": _h(json.dumps(schema, sort_keys=True)) if schema is not None else "",
            "model": str(model or ""), "effort": str(effort or ""), "cli": str(cli or "")}


def _key(fp: dict) -> str:
    return _h(json.dumps({k: fp.get(k, "") for k in PARTS}, sort_keys=True))


def _ts(entry: dict) -> float:
    ts = entry.get("ts")
    return float(ts) if isinstance(ts, (int, float)) else 0.0


def _entries(state) -> dict:
    return {k: v for k, v in state.items() if isinstance(v, dict)} if isinstance(state, dict) else {}


def signals(db, now: float) -> dict:
    """What likely_next weighs, read from the project database: running tasks, queued tasks and llm
    schedules due within NEXT_MIN (a waiting task's not_before is its retry time), the newest user
    message, and the starts of the last GAPS coordinator turns."""
    from .db import deferral
    horizon = now + NEXT_MIN * 60
    queued = db.q("SELECT * FROM tasks WHERE status='queued' AND (not_before IS NULL OR not_before<=?) "
                  "ORDER BY id", (horizon,))
    unmet = db.unmet_dependencies(queued) if queued else {}
    due = [(f"task #{t['id']}", t["not_before"]) for t in queued
           if not unmet.get(t["id"]) and "when" not in deferral(t)]
    due += [(f"schedule {s['name']}", s["next_run"]) for s in
            db.q("SELECT name, next_run FROM schedules WHERE enabled=1 AND kind='llm' AND next_run IS NOT NULL "
                 "AND next_run<=?", (horizon,))]
    chat = db.one("SELECT MAX(ts) t FROM messages WHERE direction='in' AND kind='user'")
    return {"running": [r["id"] for r in db.q("SELECT id FROM tasks WHERE status='running' ORDER BY id")],
            "due": due, "chat": chat["t"] if chat else None,
            "turns": [r["started"] for r in db.q("SELECT started FROM runs WHERE role='coordinator' AND "
                                                 "started IS NOT NULL ORDER BY started DESC LIMIT ?", (GAPS,))]}


def likely_next(sig: dict, now: float) -> tuple[bool, str]:
    """(whether a coordinator turn likely follows within the hour, why) from signals(): a task
    running (its end wakes the coordinator), a task or schedule due within NEXT_MIN, a user message
    under CHAT_MIN old, or the last GAPS turn gaps (up to now) with a median under QUIET_GAP_MIN.
    Fewer earlier turns than that count as likely: a young project is rarely quiet."""
    if sig.get("running"):
        return True, "task " + ", ".join(f"#{i}" for i in sig["running"][:3]) + " running"
    horizon = now + NEXT_MIN * 60
    due = sorted((at or now, label) for label, at in sig.get("due") or [] if not at or at <= horizon)
    if due:
        at, label = due[0]
        return True, f"{label} due" + (f" in {(at - now) / 60:.0f} min" if at > now else "")
    chat = sig.get("chat")
    if isinstance(chat, (int, float)) and 0 <= now - chat < CHAT_MIN * 60:
        return True, f"a user message {(now - chat) / 60:.0f} min ago"
    turns = sorted(t for t in sig.get("turns") or [] if isinstance(t, (int, float)) and t <= now)[-GAPS:]
    gaps = [b - a for a, b in zip(turns, turns[1:] + [now])]
    if len(gaps) < GAPS:
        return True, f"only {len(gaps)} earlier turns"
    median = statistics.median(gaps) / 60
    if median <= QUIET_GAP_MIN:
        return True, f"median turn gap {median:.0f} min"
    return False, (f"nothing running or due within {NEXT_MIN} min, no user message in {CHAT_MIN} min, "
                   f"median turn gap {median:.0f} min")


def choose(state, fp: dict, now: float, warm_min: float = WARM_MIN, signals=None) -> tuple[str, str]:
    """(ttl, reason) for a turn with prefix `fp`, given the kv state of earlier 1h turns. A cold turn
    calls `signals()` (signals(), for likely_next) and goes at 5m when no next turn looks likely;
    without it, or when it fails, a cold turn goes at 1h."""
    entries = _entries(state)
    try:
        warm_s = float(warm_min) * 60
    except (TypeError, ValueError):
        warm_s = WARM_MIN * 60
    hit = entries.get(_key(fp))
    if hit:
        age = now - _ts(hit)
        if 0 <= age < warm_s:
            return "5m", f"warm: same prefix sent at 1h {age / 60:.0f} min ago"
        reason = f"cold: same prefix last sent at 1h {max(age, 0) / 60:.0f} min ago"
    elif not entries:
        reason = "cold: no earlier 1h turn"
    else:
        last = max(entries.values(), key=_ts)
        changed = [k for k in PARTS if last.get(k, "") != fp.get(k, "")]
        reason = "changed since the last 1h turn: " + (", ".join(changed) or "prefix")
    if signals is None:
        return "1h", reason
    try:
        likely, why = likely_next(signals(), now)
    except Exception as e:   # only saves money: the turn goes at 1h as before
        return "1h", f"{reason}; next turn not estimated: {type(e).__name__}"
    return ("1h", f"{reason}; next turn likely: {why}") if likely else ("5m", f"{reason}; quiet: {why}")


def record(state, fp: dict, now: float | None = None) -> dict:
    """The kv state with a 1h turn of prefix `fp` started at `now`; keeps the newest KEEP prefixes."""
    entries = _entries(state)
    entries[_key(fp)] = {"ts": time.time() if now is None else now, **{k: fp.get(k, "") for k in PARTS}}
    newest = sorted(entries.items(), key=lambda kv: _ts(kv[1]), reverse=True)[:KEEP]
    return dict(newest)


# Agents whose prompt cache follows the session (Provider.cache_key_per_session) miss it on every
# fresh session, however stable the prefix. Their coordinator turns continue the last turn's
# session instead, while that is cheaper than starting over: same prefix and agent, ended under
# coordinator.thread_warm_min ago, under coordinator.thread_max_turns turns (under 2 turns: off),
# history under THREAD_GROWTH x its first turn's prompt, and its last continued turn read the cache.
THREAD_WARM_MIN = 10
THREAD_MAX_TURNS = 8
THREAD_GROWTH = 2.0


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pick_thread(last: dict | None, fp: dict, provider: str, now: float, cfg: dict,
                saved=lambda session: True) -> tuple[str, dict]:
    """(session to continue or "", the turn's thread note) for a coordinator turn with prefix `fp`
    (fingerprint()), given `last`, the newest finished coordinator run. `saved(session)` says the
    agent can still continue that session."""
    key = _key(fp)
    fresh = lambda why: ("", {"key": key, "turn": 1, "why": why})
    max_turns = _num(cfg.get("thread_max_turns"), THREAD_MAX_TURNS)
    if max_turns < 2:
        return fresh("off")
    if not last:
        return fresh("no earlier turn")
    try:
        prev = (json.loads(last.get("note") or "{}").get("turn") or {}).get("thread") or {}
    except (ValueError, AttributeError):
        prev = {}
    if last.get("provider") != provider or not last.get("session_id"):
        return fresh("the last turn left no session of this agent")
    if last.get("status") != "ok":
        return fresh(f"the last turn ended {last.get('status')}")
    if not isinstance(prev, dict) or prev.get("key") != key:
        return fresh("prefix changed")
    age = now - _num(last.get("ended") or last.get("started"), 0.0)
    if not 0 <= age <= _num(cfg.get("thread_warm_min"), THREAD_WARM_MIN) * 60:
        return fresh(f"cold: the last turn ended {max(age, 0) / 60:.0f} min ago")
    turn = int(_num(prev.get("turn"), 1))
    if turn >= max_turns:
        return fresh(f"thread full: {turn} turns")
    read = int(last.get("cache_read_tokens") or 0)
    sent = read + int(last.get("input_tokens") or 0) + int(last.get("cache_write_tokens") or 0)
    base = int(_num(prev.get("base"), 0)) or sent
    if sent > THREAD_GROWTH * base:
        return fresh(f"thread grew to {sent} prompt tokens from {base}")
    if turn >= 2 and read < sent / 2:
        return fresh("its last continued turn missed the cache")
    if not saved(last["session_id"]):
        return fresh("the session is no longer saved")
    return last["session_id"], {"key": key, "turn": turn + 1, "base": base, "why": "continued"}
