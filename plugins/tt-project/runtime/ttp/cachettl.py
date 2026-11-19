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


WORKER_TTLS = ("5m", "1h", "off", "auto")
WORKER_DEFAULT = "5m"   # what "auto" picks for a run no later run is likely to resume (worker_for_task)
# Workers and reviews stay at 5m, and set it: Claude Code (2.1.285) reads CLAUDE_CODE_PROMPT_CACHE_TTL
# for the main conversation, and unset it picks 1h on a Claude subscription (5m on an API key); its
# subagents take CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL, 5m unset. A run's lifetime is fixed when it
# starts, and a worker's calls mostly follow each other within seconds while its context grows, so 1h
# doubles the price of the many writes for the few reads after a gap. A replay of a day of live runs
# (75 coordinator, 86 worker, 27 review) priced each run at both lifetimes: 1h for every worker cost
# 11% more (20 cold restarts saved), 1h for every review 27% more, and even picking 1h only for the
# runs it would have paid for, known afterwards, saved 2% on workers and nothing on reviews. A resumed
# run after a wait would read its predecessor's history at 1h, but that pays only the 1h price of the
# predecessor's whole run: about even (17 resumed runs). So no start-time rule saves here.


def worker_setting(budget: dict) -> str:
    """budget.worker_cache_ttl, the prompt cache lifetime of every run but the coordinator's: "5m" (the
    default: a write at 1.25x input instead of 2x, and worker calls rarely sit 5 min apart), "1h", or
    "off" (no setting: the agent's own default). Unset, "" or an unknown value is the default."""
    v = str((budget or {}).get("worker_cache_ttl") or "").strip().lower()
    return v if v in WORKER_TTLS and v != "auto" else WORKER_DEFAULT


RESUMED_WINDOW_S = 3300   # how long after a 1h run ends its session is still read from cache (5 min short of 1 h)
WARM_WINDOW_S = 240       # the same for a 5m run


def worker_for_task(budget: dict, role: str, task: dict | None) -> tuple[str, str]:
    """(lifetime, why) of a worker or reviewer run. budget.worker_cache_ttl "5m", "1h" or "off": that. "auto" (the default) or unset: 1h for a
    worker run whose task is likely to run again on this session (a code task, which waits on its checks,
    review and landing, or any task that has waited before), so its next run resumes it from cache
    (ctxfork.own_source) instead of writing a fresh prompt; 5m otherwise. A 1h write costs 2x input
    instead of 1.25x, paid once; each warm resume then reads its history at 0.1x instead of writing it."""
    raw = str((budget or {}).get("worker_cache_ttl") or "").strip().lower()
    if raw in WORKER_TTLS and raw != "auto":
        return raw, "configured"
    if role == "worker" and task:
        from .db import load_result
        if task.get("kind") == "code":
            return "1h", "a code task runs again after its checks and review"
        try:
            waited = int(load_result(task.get("result")).get("waits") or 0) > 0
        except (TypeError, ValueError, AttributeError):
            waited = False
        if waited:
            return "1h", "it waited before, so it runs again on this session"
    return WORKER_DEFAULT, "default"


def window_s(note: dict | None) -> float:
    """How long after a run ended its session is still warm, by the lifetime its note records."""
    return RESUMED_WINDOW_S if (note or {}).get("cache_ttl") == "1h" else WARM_WINDOW_S


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
    return _cold(reason, now, signals)


def _cold(reason: str, now: float, signals) -> tuple[str, str]:
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


# A persistent coordinator session (coordsession.py) caches its whole history, not only the stateless
# prefix, so its wakes are judged on the session's own writes, kept in its kv state under SESSION:
# {"h1": start of its last 1h wake, "ttl": its last wake's lifetime, "ts": that wake's start, "end": its
# end (coordsession.record)}. A 1h request never reads a 5m write: a wake that asked for 1h right after a
# warm 5m wake re-wrote that wake's whole tail. So a resumed wake goes at 5m while the session's last 5m
# write is warm (TAIL_WARM_S since its wake ended). Every other session wake (a new session, a resume
# after a 1h wake or with its 5m tail gone) goes at 1h when a next wake is likely (likely_next), so the
# history it writes outlives the many 5-60 min gaps. A warm 1h write behind 5m wakes is not counted on:
# the cache lookback reaches only the last few blocks, so a resume after a gone 5m tail re-wrote the
# whole history even with the session's 1h write warm (recorded wakes read only the system prefix).
# A missed resume re-writes the whole history (~$0.8 at 150k tokens), a 1h write costs ~$0.09 more
# than a 5m one on the delta: 1h pays once over a third of the gaps run past 5 min, and about half do.
SESSION = "cache"
TAIL_WARM_S = 300        # a 5m write unread this long is gone


def _tail_age(c: dict, now: float) -> float | None:
    """Seconds since the session's last 5m wake ended (else started), while its write is warm."""
    stamps = [v for v in (c.get("ts"), c.get("end")) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if c.get("ttl") != "5m" or not stamps:
        return None
    age = now - max(stamps)
    return age if 0 <= age < TAIL_WARM_S else None


def choose_session(state, mode: str, now: float, warm_min: float = WARM_MIN, signals=None) -> tuple[str, str]:
    """(ttl, reason) for a session wake of `mode` ("new" or "resume"), given the session's kv state."""
    st = state if isinstance(state, dict) else {}
    c = st.get(SESSION) if isinstance(st.get(SESSION), dict) else {}
    h1 = c.get("h1")
    if mode != "resume":
        reason = "cold: a new session"
    elif (tail := _tail_age(c, now)) is not None:
        return "5m", f"warm: the session's 5m wake ended {tail / 60:.0f} min ago; a 1h request would not read it"
    elif not isinstance(h1, (int, float)):
        reason = "cold: no 1h write of this session"
    elif not 0 <= now - h1 < _num(warm_min, WARM_MIN) * 60:
        reason = f"cold: the session last wrote at 1h {max(now - h1, 0) / 60:.0f} min ago"
    elif c.get("ttl") != "1h":
        reason = f"cold: the session's 5m wakes since its 1h write {(now - h1) / 60:.0f} min ago are gone"
    else:
        reason = f"warm: the session's last wake went at 1h {(now - h1) / 60:.0f} min ago"
    return _cold(reason, now, signals)


def session_record(state, mode: str, ttl: str, now: float) -> dict:
    """The session's kv state once a wake of `mode` started at `ttl`: a new session starts its own."""
    st = dict(state) if isinstance(state, dict) else {}
    prev = st.get(SESSION) if mode == "resume" and isinstance(st.get(SESSION), dict) else {}
    st[SESSION] = {"h1": now if ttl == "1h" else prev.get("h1"), "ttl": ttl, "ts": now}
    return st


def session_ended(state, now: float) -> dict:
    """The session's kv state once its running wake ended at `now` (its writes are that fresh)."""
    st = dict(state) if isinstance(state, dict) else {}
    if isinstance(st.get(SESSION), dict):
        st[SESSION] = {**st[SESSION], "end": now}
    return st


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
