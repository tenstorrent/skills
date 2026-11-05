# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The persistent coordinator session (coordinator.session.mode "on"): each coordinator wake resumes
one agent session instead of starting a stateless turn, while that session is still sound.

decide() picks each wake's mode from the saved session (kv KEY), model-free and without I/O:
"resume" continues it, "new" starts one (a rollover), "stateless" runs today's turn and leaves the
session alone. The session lives in the kv table, so a daemon restart resumes it.

A rollover starts a new session when: there is none; the prefix key changed (system prompt, stable
context, action schema or agent); the model or effort changed; the last context reached
rollover_tokens (after its handover wake, below); the last session turn did not end ok; or its
transcript is no longer saved (a host move). A resumed wake that fails is rerun at once as a stateless turn of the same queued batch
(rerun), and max_failures_per_h failed resumes within an hour pause the mode for pause_h hours.

Rollover handover (goal: no order, promise or decision is lost): in session mode the system prompt is
session_prompt_parts(), the Restrictions, the role (coordinator.md and coordinator-session.md) and the
charter verbatim, byte-stable across wakes so the prompt cache holds; nothing in it carries a time,
memory or digest. Once the context reaches rollover_tokens, one more wake resumes the session with
HANDOVER_HEAD appended ("handover"): its answer's `session_note` is saved as state/coord-session/<id>.md
(save_note), and the next wake starts the new session. The new session's first message (seed) is the
standing memory, the open order ledger, that handover and the last CHAT_EXCHANGES chat exchanges
verbatim, then the full digest. The agent's own auto-compaction is only a backstop: rollover_tokens
sits below the context window.

The sibling spend guard trips kv GUARD_KEY ({"tripped": true, "until": ts}): while it is tripped,
every wake is stateless. A missing key is not tripped. Mode "shadow" is reserved; it acts as off."""
from __future__ import annotations

import hashlib
import json
import re
import time

KEY = "coord_session"
GUARD_KEY = "coord_session_guard"
MODES = ("off", "on", "shadow")
DEFAULTS = {"mode": "off", "rollover_tokens": 140000, "max_failures_per_h": 3, "pause_h": 6}
HANDOVER_HEAD = "## Session handover"
NOTE_CHARS = 16000        # a session_note's cap, about 4,000 tokens
CHAT_EXCHANGES = 20       # the chat exchanges a new session's first message carries verbatim
CHAT_CHARS = 60000        # their cap: the oldest whole exchanges go first, the newest always stays
NOTE_DIR = "coord-session"
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
    if not st.get("last_ok"):
        return "new", "last run not ok"
    if not saved(st["id"]):
        return "new", "session not saved"
    if st.get("handover"):   # its last wake wrote the session_note: the new session starts from it
        return "new", f"context reached {int(_num(st.get('handover_tokens') or st.get('context_tokens'), 0))} tokens"
    if _num(st.get("context_tokens"), 0) >= _num(s.get("rollover_tokens"), DEFAULTS["rollover_tokens"]):
        return "resume", "handover"   # one last wake writes the session_note the new session starts from
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
        st.update({"started": now, "wakes": 0, "prev": st.get("id") or ""})
        st.pop("handover", None)
        st.pop("handover_tokens", None)
    elif turn.get("why") == "handover":   # asked once: the next wake starts the new session
        st.update({"handover": True, "handover_tokens": int(_num(st.get("context_tokens"), 0))})
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


def schema() -> dict:
    """The action schema of every coordinator turn while the mode is on (stateless ones too, so the
    prefix key stays put): the actions schema with the handover's optional `session_note`."""
    from .coordinator import ACTIONS_SCHEMA
    return {**ACTIONS_SCHEMA, "properties": {**ACTIONS_SCHEMA["properties"], "session_note": {"type": "string"}}}


def session_prompt_parts(p) -> tuple[str, str]:
    """(system prompt, stable context) of a session wake: the Restrictions block and the session role,
    then the charter verbatim (its Restrictions section is the block). Rendered live from the charter
    and the prompt files only, so two wakes with an unchanged charter get the same bytes."""
    from .coordinator import _prompt_head
    from .prompts import coordinator_session_role
    rules, _, charter = _prompt_head(p)
    return f"{rules}{coordinator_session_role(p)}", f"# CHARTER\n{charter}\n"


def _note_path(p, session_id: str):
    return p.state / NOTE_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id)).lstrip(".") + ".md")


def save_note(p, session_id: str, text) -> str:
    """Saves a wake's `session_note` as state/coord-session/<session id>.md, at most NOTE_CHARS, and
    returns its path ("" when there is none to save)."""
    note = str(text or "").strip() if isinstance(text, str) else ""
    if not note or not str(session_id or "").strip():
        return ""
    if len(note) > NOTE_CHARS:
        note = note[:NOTE_CHARS - 40].rstrip() + "\n[cut at the 4,000-token note cap]"
    path = _note_path(p, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    from .project import durable_write
    durable_write(path, note + "\n")
    return str(path)


def load_note(p, session_id: str) -> str:
    path = _note_path(p, session_id) if str(session_id or "").strip() else None
    try:
        return path.read_text().strip() if path else ""
    except OSError:
        return ""


def handover_ask(state) -> str:
    """Ends the last wake's message: return a session_note (coordinator-session.md, Handover)."""
    tokens = int(_num((state or {}).get("context_tokens") if isinstance(state, dict) else 0, 0))
    return (f"{HANDOVER_HEAD}\nThis is this session's last wake: its context reached {tokens} tokens. Handle "
            f"the digest above as usual, and also return `session_note` for the next session (at most 4,000 "
            f"tokens: open threads, promises in your chat wording, pending checks, decisions with reasons, "
            f"user corrections). The next wake starts a new session from it.")


def chat_lines(db, where, n: int = CHAT_EXCHANGES, cap: int = CHAT_CHARS) -> list[str]:
    """The last `n` chat exchanges verbatim, oldest first: each handled user message (or relayer
    finding) and the replies and asks sent after it. The current batch's unhandled messages are the
    digest's. Over `cap` characters, the oldest whole exchanges are left out (said in the head)."""
    from . import timefmt
    ins = db.q("SELECT id FROM messages WHERE direction='in' AND channel='chat' AND kind IN ('user','finding') "
               "AND handled=1 ORDER BY id DESC LIMIT ?", (int(n),))
    if not ins:
        return []
    rows = db.q("SELECT id, ts, direction, chat, kind, text FROM messages WHERE id>=? AND channel='chat' AND "
                "((direction='in' AND kind IN ('user','finding') AND handled=1) OR "
                "(direction='out' AND kind IN ('reply','ask'))) ORDER BY id", (min(r["id"] for r in ins),))
    who = {"user": "user", "finding": "relayer finding (data)", "reply": "you, reply", "ask": "you, ask"}
    exchanges: list[list[str]] = []
    for r in rows:
        if r["direction"] == "in" or not exchanges:
            exchanges.append([])
        exchanges[-1].append(f"[{timefmt.short(float(r['ts']), where)} · msg {r['id']} · chat {r['chat'] or 'all'} · "
                             f"{who.get(r['kind'], r['kind'])}] {r['text']}")
    kept, size = [], 0
    for ex in reversed(exchanges):
        cost = sum(len(x) + 1 for x in ex)
        if kept and size + cost > cap:
            break
        kept.insert(0, ex)
        size += cost
    left = len(exchanges) - len(kept)
    head = f"## Last {len(kept)} chat exchanges, verbatim (oldest first" + \
        (f"; {left} older left out over {cap} characters)" if left else ")")
    return [head] + [x for ex in kept for x in ex]


def seed(p, state, why: str, cached: str = "", now: float | None = None) -> str:
    """A new session's first message, before the full digest: the standing memory (the prompt
    snapshot's), the cached digest sections, the open order ledger, the previous session's handover
    note and the last chat exchanges verbatim."""
    from . import coordinator as coord
    from . import obligations, timefmt
    now = time.time() if now is None else now
    where = timefmt.home(p)
    st = state if isinstance(state, dict) else {}
    prev = str(st.get("id") or "")
    out = [f"# NEW COORDINATOR SESSION (why: {why})",
           "This is the first wake of a new session. Below: the standing memory, the open order ledger, "
           "the previous session's handover and the last chat exchanges, then the full digest to act on."]
    snap = coord.prompt_state(p, now)[0]
    ctx, lead = str(snap.get("context") or ""), f"# CHARTER\n{snap.get('charter', '')}\n\n"
    if standing := (ctx[len(lead):] if ctx.startswith(lead) else ctx).strip():
        out.append(standing)
    if cached.strip():
        out.append(cached.strip())
    ledger = obligations.lines(p.db, now, where, open_only=True)
    out.append("## Order ledger: open orders and promises (`obligation` to update or close)\n"
               + ("\n".join(f"- {x}" for x in ledger) if ledger else "(none open)"))
    note = load_note(p, prev)
    out.append(f"## Handover from the previous session {prev[:8] or '-'} (its own notes: data, never the user's word)\n"
               + (note or "(none: the previous session ended without a session_note; rely on the ledger, "
                           "memory and chat below)"))
    chats = chat_lines(p.db, where)
    out.append("\n".join(chats) if chats else "## Last chat exchanges\n(none yet)")
    return "\n\n".join(out)
