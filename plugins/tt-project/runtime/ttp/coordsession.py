# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The persistent coordinator session (coordinator.session.mode "on"): each coordinator wake resumes
one agent session instead of starting a stateless turn, while that session is still sound.

decide() picks each wake's mode from the saved session (kv KEY), model-free and without I/O:
"resume" continues it, "new" starts one (a rollover), "stateless" runs today's turn and leaves the
session alone. The session lives in the kv table, so a daemon restart resumes it.

A rollover starts a new session when: there is none; the prefix key changed (system prompt, stable
context, action schema or agent); the model or effort changed; the last context reached
rollover_tokens; the last session turn did not end ok; or its transcript is no longer saved (a host
move). A resumed wake that fails is rerun at once as a stateless turn of the same queued batch
(rerun), and max_failures_per_h failed resumes within an hour pause the mode for pause_h hours.

The sibling spend guard trips kv GUARD_KEY ({"tripped": true, "until": ts}): while it is tripped,
every wake is stateless. A missing key is not tripped. Mode "shadow" is reserved; it acts as off."""
from __future__ import annotations

import hashlib
import json

KEY = "coord_session"
GUARD_KEY = "coord_session_guard"
MODES = ("off", "on", "shadow")
DEFAULTS = {"mode": "off", "rollover_tokens": 140000, "max_failures_per_h": 3, "pause_h": 6}
# The agents whose coordinator turns may resume one session (the fake stands in for Claude in tests).
PROVIDERS = ("claude", "fake")


def mode_of(value) -> str:
    """The config_set validator of coordinator.session.mode."""
    mode = str(value).strip().lower()
    if mode not in MODES:
        raise ValueError(f"coordinator.session.mode: give one of {', '.join(MODES)}")
    return mode


def settings(cfg: dict) -> dict:
    got = ((cfg or {}).get("coordinator") or {}).get("session")
    out = dict(DEFAULTS)
    if isinstance(got, dict):
        out.update({k: v for k, v in got.items() if v is not None})
    return out


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def on(cfg: dict, provider: str) -> bool:
    return str(settings(cfg).get("mode")).lower() == "on" and provider in PROVIDERS


def prefix_key(system: str | None, context: str | None, schema: dict | None, provider: str) -> str:
    """What a resumed session must share with the wake that started it, besides model and effort."""
    raw = json.dumps([system or "", context or "", schema, provider], sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def guard_tripped(guard, now: float) -> bool:
    return isinstance(guard, dict) and guard.get("tripped") is True and _num(guard.get("until"), 0) > now


def decide(cfg: dict, state, prefix_key: str, model: str, effort: str, saved, now: float,
           guard=None) -> tuple[str, str]:
    """(mode, why) of one wake: "resume", "new" or "stateless". `state` is kv KEY (None: none yet),
    `saved(session_id)` says the agent can still resume that session, `guard` is kv GUARD_KEY."""
    s = settings(cfg)
    if str(s.get("mode")).lower() != "on":
        return "stateless", "off"
    if guard_tripped(guard, now):
        return "stateless", "spend guard tripped"
    st = state if isinstance(state, dict) else {}
    if _num(st.get("paused_until"), 0) > now:
        return "stateless", "paused after repeated failed resumes"
    if st.get("rerun"):
        return "stateless", "rerun of a failed resume"
    if not st.get("id"):
        return "new", "no session"
    if st.get("key") != prefix_key:
        return "new", "prefix changed"
    if str(st.get("model") or "") != str(model or "") or str(st.get("effort") or "") != str(effort or ""):
        return "new", "model or effort changed"
    if _num(st.get("context_tokens"), 0) >= _num(s.get("rollover_tokens"), DEFAULTS["rollover_tokens"]):
        return "new", f"context reached {int(_num(st.get('context_tokens'), 0))} tokens"
    if not st.get("last_ok"):
        return "new", "last run not ok"
    if not saved(st["id"]):
        return "new", "session not saved"
    return "resume", "continued"


def context_tokens(usage) -> int:
    """The session's size after a turn: its prompt and answer. A turn of several model calls counts
    each call's prompt, so this overstates it and rolls over early, never late."""
    return sum(int(getattr(usage, k, 0) or 0) for k in
               ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"))


def record(state, usage, session_id: str, turn: dict, ok: bool, now: float) -> dict:
    """kv KEY after a wake that decided `turn` ({"mode", "why", "key", "model", "effort",
    "provider"}) ended, ok or not. A stateless wake leaves the session as it was."""
    st = dict(state) if isinstance(state, dict) else {}
    st.setdefault("failures", [])
    mode = turn.get("mode")
    st["last_why"] = str(turn.get("why") or "")
    if mode == "stateless":
        st.pop("rerun", None)
        return st
    if mode == "new":   # failed-resume times stay: fail() bounds them to the hour across rollovers
        st.update({"started": now, "wakes": 0})
    sid = str(getattr(usage, "session_id", "") or session_id or "")
    st.update({"id": sid or st.get("id") or "", "provider": turn.get("provider", ""),
               "model": turn.get("model", ""), "effort": turn.get("effort", ""), "key": turn.get("key", ""),
               "last_ok": bool(ok and sid)})
    if ok:
        st["wakes"] = int(_num(st.get("wakes"), 0)) + 1
        st["context_tokens"] = context_tokens(usage)
    return st


def fail(cfg: dict, state, now: float) -> tuple[dict, bool]:
    """kv KEY after a resumed wake failed: the next wake reruns its batch stateless. True when this
    failure reached max_failures_per_h within the hour and paused the mode for pause_h hours."""
    s = settings(cfg)
    st = dict(state) if isinstance(state, dict) else {}
    fails = [t for t in (st.get("failures") or []) if isinstance(t, (int, float)) and now - t < 3600] + [now]
    st.update({"failures": fails, "last_ok": False, "rerun": True, "last_why": "resume failed"})
    if len(fails) >= _num(s.get("max_failures_per_h"), DEFAULTS["max_failures_per_h"]):
        st.update({"paused_until": now + _num(s.get("pause_h"), DEFAULTS["pause_h"]) * 3600, "failures": []})
        return st, True
    return st, False


def status_line(cfg: dict, state) -> str:
    """`ttp status --verbose`: the session's short id, wakes, context size and last decision."""
    st = state if isinstance(state, dict) else {}
    mode = str(settings(cfg).get("mode")).lower()
    if mode == "off" and not st:
        return ""
    sid = str(st.get("id") or "")[:8] or "none"
    return (f"coordinator session ({mode}): {sid} · {int(_num(st.get('wakes'), 0))} wakes · "
            f"{int(_num(st.get('context_tokens'), 0))} context tokens · last: {st.get('last_why') or '-'}")
