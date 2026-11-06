# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The persistent coordinator session (coordinator.session.mode "on", the default): each coordinator wake resumes
one agent session instead of starting a stateless turn, while that session is still sound.

decide() picks each wake's mode from the saved session (kv KEY), model-free and without I/O:
"resume" continues it, "new" starts one (a rollover), "stateless" runs today's turn and leaves the
session alone. The session lives in the kv table, so a daemon restart resumes it.

A rollover starts a new session when: there is none; the prefix key changed (system prompt, stable
context, action schema or agent); the model or effort changed; the last context reached limit()
(after its handover wake, below); the last session turn did not end ok; or its transcript is no
longer saved (a host move). The context is the last model call's prompt and answer, and the limit
is the session's first prompt plus rollover_growth_tokens, at most the model's context window less
rollover_margin_tokens, so a project with a large prompt still resumes; rollover_tokens above 0 sets
a fixed limit instead. A resumed wake that fails is rerun at once as a stateless turn of the same
queued batch (rerun), and max_failures_per_h failed resumes within an hour pause the mode for
pause_h hours.

Rollover handover (goal: no order, promise or decision is lost): in session mode the system prompt is
session_prompt_parts(), the Restrictions, the role (coordinator.md and coordinator-session.md) and the
charter verbatim, byte-stable across wakes so the prompt cache holds; nothing in it carries a time,
memory or digest. Once the context reaches limit(), one more wake resumes the session with
HANDOVER_HEAD appended ("handover"): its answer's `session_note` is saved as state/coord-session/<id>.md
(save_note), and the next wake starts the new session. The new session's first message (seed) is the
standing memory, the open order ledger, that handover and the last CHAT_EXCHANGES chat exchanges
verbatim, then the full digest. The agent's own auto-compaction is only a backstop: limit()
sits below a known context window.

Memory between wakes: the digest's memory deltas compare live memory with the prompt snapshot, so they
reset when the snapshot re-renders, and a session never sees what was recorded between its last wake
and that re-render. kv KEY's `sent` holds a hash of each entry the session was sent (memory_view: the
standing memory, the STANDING block and the cached sections user_corrections and memory_recent). Each
resumed wake leads with the entries added, changed or retired since (resume_memory), and a wake that
ends ok makes its `pending` map the new `sent`; one that fails keeps the old map, so they are sent again.

The sibling spend guard trips kv GUARD_KEY ({"tripped": true, "until": ts}): while it is tripped,
every wake is stateless. A missing key is not tripped. Mode "shadow" is reserved; it acts as off.

The delta digest: a resumed wake sends only the digest sections whose text changed since the
session's last acknowledged wake (one that ended ok), each whole, and every new event, plus one line
naming the sections it leaves out and the ones gone. The section hashes that wake was sent are kept in
KEY (DIGEST), so they are on disk. The whole digest, the turn's one build without the per-turn
collapsing (coordinator.COLLAPSIBLE, rendered beside the collapsed parts), goes to a new session (a rollover), and to a resumed one whenever the
saved hashes may not match what it saw: none saved, a stateless turn or a failed or compacted wake
since, another turn's digest since (the collapse baseline moved), a daemon restart, or the session
asked for it with the action {"type": "noop", "key": FULL_ASK}.

Per-row updates (coordinator.session.row_delta, on by default): only a changed Open tasks section
(ROWS, rows keyed '- #<id> |') or Course check section (COURSE, rows keyed by their sign and task id,
'- costly: #<id>', and its count line '- costly: <n> more') is compared row by row, each row without its ages and read times (TICKS),
against the row hashes the acknowledged wake saved. It sends its header, a line naming the rows added,
changed and removed by key and counting the rest, then the added and changed rows, each whole with its
current age. Never a partial row or a text diff. The section goes whole, as without per-row updates,
when: a key repeats (a group line is keyed by its hash), the list is cut short by its row limit
(cut_line()), its header changed in more than its counts, more than half its rows changed, the rows it
kept changed order, there are no row hashes acknowledged, or the whole section is shorter. A task the
section no longer shows is called no longer open (a Course check row: no longer shown) unless a group
line still names it. Every other
section (memory, user corrections, promises, chats) is hashed raw and sent whole on any change, so one
whose only change is an age goes whole too."""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import cachettl

KEY = "coord_session"
GUARD_KEY = "coord_session_guard"
MODES = ("off", "on", "shadow")
DEFAULTS = {"mode": "on", "rollover_tokens": 0, "rollover_growth_tokens": 100000, "context_window_tokens": 0,
            "rollover_margin_tokens": 40000, "max_failures_per_h": 3, "pause_h": 6, "row_delta": True}
LONG_WINDOW = 1_000_000   # a model id ending "[1m]" (Claude's long-context variant)
HANDOVER_HEAD = "## Session handover"
MEMORY_HEAD = ("## Memory changed since this session last saw it (a changed entry replaces its earlier line; "
               "a retired one no longer applies)")
STANDING = "# STANDING"   # memory_view's key of the STANDING block (no memory entry's name starts with #)
NOTE_CHARS = 16000        # a session_note's cap, about 4,000 tokens
CHAT_EXCHANGES = 20       # the chat exchanges a new session's first message carries verbatim
CHAT_CHARS = 60000        # their cap: the oldest whole exchanges go first, the newest always stays
NOTE_DIR = "coord-session"
# Session mode runs on every provider, with no per-provider opt-out. Each provider says how it keeps
# the session (providers/base.Provider.coord_session): "agent" resumes the agent CLI's own saved session
# (claude, chat, which is Claude Code on a served model, codex; the fake in tests); a provider that
# cannot yet runs each wake stateless, its reason (Provider.session_gap) named in the wake's `why`.
DIGEST = "digest"   # in KEY: {"sections": {key: [hash, label]}, "keyed": {key: {"head": hash, "rows": {row key:
#                     hash}}}, "seen": collapse baseline, "gen": daemon start}
FULL_ASK = "full_digest"
ALWAYS = ("events",)   # digest sections every wake sends
HEAD = "budget"        # its first line is the STATE time header, sent every wake; the rest is hashed
DELTA = "session_delta"
ROWS = "tasks"         # the Open tasks section (coordinator.digest_parts)
COURSE = "course_check"   # the Course check section (course.py)
# Ages and read times, left out of what is hashed of a row (coordinator.collapse_rows uses the same rule).
TICKS = re.compile(r"\b\d+(?:\.\d+)?\s?(?:h|min|m|s)\b")
_READ_RE = re.compile(r"\(read [^()]*\)")
OPEN_ROWS = 60   # the Open tasks section's row limit
CUT_NAMED = 40   # the most open tasks past it named one by one
_CUT = "- (open tasks past this list's limit of"
CHATS_SHOWN = 10   # the Chats attached section's row limit (it has no line for the chats past it)
_ROW_KEY = re.compile(r"- (#\d+) \| ")   # an Open tasks row's key, its task id
_COURSE_KEY = re.compile(r"- ([a-z][a-z ]*): (#\d+|\d+ more)\b")   # a Course check row's sign and task id


def _row_key(section: str, line: str) -> str | None:
    if section == ROWS:
        m = _ROW_KEY.match(line)
        return m[1] if m else None
    m = _COURSE_KEY.match(line)
    return (f"{m[1]} {m[2] if m[2].startswith('#') else 'more'}") if m else None


def cut_line(ids: list[int]) -> str:
    """The Open tasks line naming the open tasks past OPEN_ROWS (the first CUT_NAMED of them)."""
    rest = len(ids) - CUT_NAMED
    return (f"{_CUT} {OPEN_ROWS} rows, {len(ids)}: {', '.join(f'#{i}' for i in ids[:CUT_NAMED])}"
            + (f" and {rest} more)" if rest > 0 else ")"))


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


def on(cfg: dict, provider: str = "") -> bool:
    """Session mode is on, whatever the provider (decide() names a provider that cannot keep one)."""
    return str(settings(cfg).get("mode")).lower() == "on"


def gap(prov) -> str:
    """Why `prov` (a providers.Provider) runs coordinator wakes stateless in session mode; "" when it keeps one."""
    return "" if getattr(prov, "coord_session", "") else f"{prov.name}: {prov.session_gap}"


def prefix_key(system: str | None, context: str | None, schema: dict | None, provider: str) -> str:
    """What a resumed session must share with the wake that started it, besides model and effort."""
    raw = json.dumps([system or "", context or "", schema, provider], sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def guard_tripped(guard, now: float) -> bool:
    return isinstance(guard, dict) and guard.get("tripped") is True and _num(guard.get("until"), 0) > now


def decide(cfg: dict, state, prefix_key: str, model: str, effort: str, saved, now: float,
           guard=None, no_session: str = "") -> tuple[str, str]:
    """(mode, why) of one wake: "resume", "new" or "stateless". `state` is kv KEY (None: none yet),
    `saved(session_id)` says the agent can still resume that session, `guard` is kv GUARD_KEY,
    `no_session` is gap() of the wake's provider."""
    s = settings(cfg)
    if str(s.get("mode")).lower() != "on":
        return "stateless", "off"
    if no_session:
        return "stateless", no_session
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
    if _num(st.get("context_tokens"), 0) >= limit(cfg, st, model):
        return "resume", "handover"   # one last wake writes the session_note the new session starts from
    return "resume", "continued"


def limit(cfg: dict, state, model: str = "") -> float:
    """The context size at which the session rolls over: rollover_tokens when above 0, else the
    session's first prompt (base_tokens) plus rollover_growth_tokens, at most the context window
    (context_window_tokens; 0: LONG_WINDOW for a "[1m]" model, else unknown) less rollover_margin_tokens."""
    s = settings(cfg)
    fixed = _num(s.get("rollover_tokens"), 0)
    if fixed > 0:
        return fixed
    st = state if isinstance(state, dict) else {}
    out = _num(st.get("base_tokens"), 0) + _num(s.get("rollover_growth_tokens"), DEFAULTS["rollover_growth_tokens"])
    window = _num(s.get("context_window_tokens"), 0) or (LONG_WINDOW if str(model or "").endswith("[1m]") else 0)
    if window > 0:
        out = min(out, window - _num(s.get("rollover_margin_tokens"), DEFAULTS["rollover_margin_tokens"]))
    return out


def _telemetry(usage, key: str) -> int:
    v = ((getattr(usage, "extra", None) or {}).get("cache_telemetry") or {})
    v = v.get(key) if isinstance(v, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def context_tokens(usage) -> int:
    """The session's size after a turn: its last model call's prompt and answer. Without per-call
    usage, the turn's sum, which overstates a turn of several calls and rolls over early, never late."""
    keys = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    return _telemetry(usage, "last_context") or sum(int(getattr(usage, k, 0) or 0) for k in keys)


def record(state, usage, session_id: str, turn: dict, ok: bool, now: float) -> dict:
    """kv KEY after a wake that decided `turn` ({"mode", "why", "key", "model", "effort",
    "provider"}) ended, ok or not. A stateless wake leaves the session as it was."""
    st = dict(state) if isinstance(state, dict) else {}
    st.setdefault("failures", [])
    mode = turn.get("mode")
    st["last_why"] = str(turn.get("why") or "")
    ack = turn.get("digest") if ok and isinstance(turn.get("digest"), dict) else None
    st.pop(DIGEST, None)   # a stateless, failed or unacknowledged wake: the next session wake sends it whole
    pending = st.pop("pending", None)   # this wake's memory_view hashes: sent once it ended ok
    if mode == "stateless":
        st.pop("rerun", None)
        return st
    st = cachettl.session_ended(st, now)   # the next wake judges this one's 5m write on it
    if ack:
        st[DIGEST] = ack
    if mode == "new":   # failed-resume times stay: fail() bounds them to the hour across rollovers
        st.update({"started": now, "wakes": 0, "prev": st.get("id") or ""})
        st.pop("handover", None)
        st.pop("base_tokens", None)
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
        if mode == "new":   # the fresh session's prompt: limit() allows growth over it
            st["base_tokens"] = _telemetry(usage, "first_context")
        if isinstance(pending, dict):
            st["sent"] = pending
    return st


def fail(cfg: dict, state, now: float) -> tuple[dict, bool]:
    """kv KEY after a resumed wake failed: the next wake reruns its batch stateless. True when this
    failure reached max_failures_per_h within the hour and paused the mode for pause_h hours."""
    s = settings(cfg)
    st = dict(state) if isinstance(state, dict) else {}
    fails = [t for t in (st.get("failures") or []) if isinstance(t, (int, float)) and now - t < 3600] + [now]
    st.update({"failures": fails, "last_ok": False, "rerun": True, "last_why": "resume failed"})
    st.pop(DIGEST, None)
    st.pop("pending", None)
    if len(fails) >= _num(s.get("max_failures_per_h"), DEFAULTS["max_failures_per_h"]):
        st.update({"paused_until": now + _num(s.get("pause_h"), DEFAULTS["pause_h"]) * 3600, "failures": []})
        return st, True
    return st, False


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:16]


def fingerprint(seen) -> str:
    """The per-turn collapse baseline (coordinator.DIGEST_SEEN_KEY) as one hash."""
    return _sha(json.dumps(seen or {}, sort_keys=True, default=str))


def _keyed(parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """`parts` with a repeated section key numbered, so each part has a hash of its own."""
    count: dict = {}
    out = []
    for key, text in parts:
        count[key] = count.get(key, 0) + 1
        out.append((key if count[key] == 1 else f"{key}#{count[key]}", text))
    return out


def _label(text: str) -> str:
    first = text.lstrip("\n").split("\n", 1)[0].lstrip("#").strip()
    return re.split(r"[:(]", first)[0].strip()[:60] or "?"


def _unticked(text: str) -> str:
    return _READ_RE.sub("(read)", TICKS.sub("", text))


def _body(key: str, text: str) -> str:
    """What is hashed of a section: HEAD without its time header line, ROWS and COURSE without ages."""
    if key == HEAD:
        return text.split("\n", 1)[1] if "\n" in text else ""
    return _unticked(text) if key in (ROWS, COURSE) else text


def sections(parts: list[tuple[str, str]]) -> dict:
    """{key: [hash, label]} of a digest's (section, text) parts."""
    return {k: [_sha(_body(k, t)), _label(_body(k, t) or t)] for k, t in _keyed(parts)}


def _key_rows(text: str, section: str = ROWS) -> list[tuple[str, str]] | None:
    """A keyed section's (row key, line) after its header (a line with no key, such as a group line, is
    keyed '~' and its hash), or None when a key repeats or the list is cut short by its row limit."""
    out = []
    for ln in text.split("\n")[1:]:
        if not ln.strip():
            continue
        if ln.startswith(_CUT):
            return None
        out.append((_row_key(section, ln) or f"~{_sha(_unticked(ln))}", ln))
    keys = [k for k, _ in out]
    return None if len(set(keys)) < len(keys) else out


def _head(text: str) -> str:
    """A section's header line without its counts, hashed."""
    return _sha(re.sub(r"\d+", "#", text.split("\n", 1)[0]))


def rows(parts: list[tuple[str, str]]) -> dict:
    """{section: {"head": _head(), "rows": {row key: hash without ages}}} of the Open tasks and Course
    check sections when their rows have stable keys, in row order."""
    out = {}
    for key, text in _keyed(parts):
        got = _key_rows(text, key.split("#")[0]) if key.split("#")[0] in (ROWS, COURSE) else None
        if got is not None:
            out[key] = {"head": _head(text), "rows": {k: _sha(_unticked(ln)) for k, ln in got}}
    return out


def _row_delta(key: str, text: str, before) -> str | None:
    """A changed Open tasks or Course check section as its header, a line naming the rows added, changed and removed by key,
    then the added and changed rows, each whole; None when it goes whole (_key_rows, a header changed in
    more than its counts, more than half its rows changed, or the rows it kept in another order)."""
    now = _key_rows(text, key.split("#")[0])
    old = before.get("rows") if isinstance(before, dict) and before.get("head") == _head(text) else None
    if now is None or not isinstance(old, dict):
        return None
    hashes = {k: _sha(_unticked(ln)) for k, ln in now}
    if [k for k, _ in now if k in old] != [k for k in old if k in hashes]:
        return None
    added = [(k, ln) for k, ln in now if k not in old]
    changed = [(k, ln) for k, ln in now if k in old and old[k] != hashes[k]]
    removed = [k for k in old if k not in hashes]
    if 2 * (len(added) + len(changed) + len(removed)) > max(len(now), len(old)):
        return None
    grouped = " ".join(ln for k, ln in now if k.startswith("~"))

    def named(got: list, what: str) -> str:
        keys, other = [k for k in got if not k.startswith("~")], sum(1 for k in got if k.startswith("~"))
        return "; ".join(([f"{what} {', '.join(keys)}"] if keys else [])
                         + ([f"{what.split(' ')[0]} {other} other line{'s' * (other > 1)}"] if other else []))
    # A task folded into a group line is not gone: that line names it.
    gone = [k for k in removed if not (k.startswith("#") and re.search(rf"{re.escape(k)}\b", grouped))]
    said = [named([k for k, _ in added], "added"), named([k for k, _ in changed], "changed"),
            named(gone, "removed (no longer open):" if key.split("#")[0] == ROWS else "removed (no longer shown):"),
            f"{len(now) - len(added) - len(changed)} as last shown, not repeated, their ages grown since"]
    note = "(rows since your last wake, by key: " + "; ".join(s for s in said if s) + ")"
    return "\n".join([text.split("\n", 1)[0], note] + [ln for k, ln in now if (k, ln) in added + changed])


def row_delta_of(value) -> bool:
    """The config_set validator of coordinator.session.row_delta."""
    got = str(value).strip().lower()
    if got not in ("true", "on", "yes", "1", "false", "off", "no", "0"):
        raise ValueError("coordinator.session.row_delta: give true or false")
    return got in ("true", "on", "yes", "1")


def row_delta_on(cfg: dict) -> bool:
    """coordinator.session.row_delta: per-row updates of the Open tasks section (on unless set off)."""
    return str(settings(cfg).get("row_delta")).strip().lower() not in ("false", "off", "no", "0")


def delta(parts: list[tuple[str, str]], state, seen_fp: str, gen: str,
          row_delta: bool = True) -> tuple[list | None, str]:
    """(the parts a resumed wake sends, why), or (None, why) when it must send the whole digest.
    `seen_fp`: fingerprint() of the collapse baseline this digest was built on; `gen`: the daemon's;
    `row_delta`: False sends every changed section whole."""
    got = (state if isinstance(state, dict) else {}).get(DIGEST)
    if not isinstance(got, dict) or not isinstance(got.get("sections"), dict):
        return None, "no acknowledged digest"
    if got.get("gen") != gen:
        return None, "daemon restarted"
    if got.get("seen") != seen_fp:
        return None, "another turn's digest since"
    before, now = got["sections"], sections(parts)
    keyed = got.get("keyed") if row_delta and isinstance(got.get("keyed"), dict) else {}
    sent, same, by_row = [], [], []
    for key, text in _keyed(parts):
        old = before.get(key)
        changed = key in ALWAYS or not isinstance(old, list) or old[:1] != now[key][:1]
        rowed = (_row_delta(key, text, keyed.get(key)) if changed and key.split("#")[0] in (ROWS, COURSE)
                 and isinstance(old, list) else None)
        if rowed is not None and len(rowed) < len(text):   # else the whole section is the shorter
            sent.append((key, rowed))
            by_row.append(now[key][1])
        elif changed:
            sent.append((key, text))
        else:
            same.append(now[key][1])
            if key == HEAD:
                sent.append((key, text.split("\n", 1)[0]))
    gone = [str(v[1]) for k, v in before.items() if k not in now and isinstance(v, list) and len(v) > 1]
    line = ("## Since your last wake in this session only the changed sections follow, each whole"
            + (f" (by row, as their key line says: {', '.join(by_row)}). " if by_row else ". ")
            + f"Unchanged, not repeated: {', '.join(same) or 'none'}"
            + (f". Gone (now empty): {', '.join(gone)}" if gone else "")
            + f'. For the whole digest on the next wake, add the action {{"type": "noop", "key": "{FULL_ASK}"}}.')
    at = 1 if sent and sent[0][0] == HEAD else 0
    return sent[:at] + [(DELTA, line)] + sent[at:], f"delta: {len(same)} unchanged, {len(gone)} gone"


def wake_digest(mode: str, state, parts: list[tuple[str, str]], full: list[tuple[str, str]], seen_fp: str,
                shown_fp: str, gen: str, row_delta: bool = True) -> tuple[list[tuple[str, str]], dict, str]:
    """(the digest parts a session wake sends, the ack record() saves once it ends ok, why).
    `parts`: the digest as built for the turn; `full`: the same build without the per-turn collapsing
    (coordinator.digest_parts' `whole`, never a second build); `shown_fp`: fingerprint() of the
    baseline this turn leaves for the next; `row_delta`: row_delta_on()."""
    sent, why = delta(parts, state, seen_fp, gen, row_delta) if mode == "resume" else (None, "new session")
    if sent is None:
        parts = list(full)
        sent, why = parts, f"full: {why}"
    return sent, {"sections": sections(parts), "keyed": rows(parts), "seen": shown_fp, "gen": gen}, why


def asked_full(actions) -> bool:
    """The session asked for the whole digest on its next wake."""
    return any(isinstance(a, dict) and a.get("type") == "noop" and a.get("key") == FULL_ASK
               for a in actions or [])


def status_line(cfg: dict, state) -> str:
    """`ttp status --verbose`: the session's short id, wakes, context size and last decision."""
    st = state if isinstance(state, dict) else {}
    if ((cfg or {}).get("core_provider") or "claude") not in PROVIDERS:
        return ""   # this coordinator's provider never runs a session
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


def memory_view(p, now: float | None = None) -> dict:
    """{key: [hash, line]} of what a new session's seed sends from the prompt snapshot now: each
    standing memory entry, the STANDING block and each entry of the cached sections (user_corrections,
    memory_recent)."""
    from . import coordinator as coord
    snap, live = coord.prompt_state(p, time.time() if now is None else now)
    out = {}
    if standing := str(snap.get("standing") or ""):
        out[STANDING] = [coord._sha(standing), f"## STANDING now reads\n{standing}"]
    taken = snap.get("entries") or {}
    out.update({e["name"]: [live["shas"][e["name"]], e["line"]] for e in live["entries"] if e["name"] in taken})
    recent = snap.get("recent") or {}
    for names, lines in ((recent.get("entries") or {}, recent.get("lines") or []),
                         (snap.get("corrections") or {}, snap.get("correction_lines") or [])):
        by_sha = {coord._sha(x): x for x in lines}
        out.update({n: [sha, by_sha[sha]] for n, sha in names.items() if sha in by_sha})
    return out


def resume_memory(p, state, prompt: str, now: float | None = None) -> tuple[str, dict]:
    """(MEMORY_HEAD block, or "" when nothing changed; this wake's `pending` hashes): the memory_view
    entries added or changed since kv KEY's `sent` and the ones retired from memory. A line this wake's
    digest already carries is not repeated. A session without `sent` (seeded before it) gets them all once.
    An entry that only left the view (aged out of the recent sections) is still in memory: not retired."""
    view = memory_view(p, now)
    sent = (state or {}).get("sent") if isinstance(state, dict) else None
    sent = sent if isinstance(sent, dict) else {}
    lines = [line for k, (sha, line) in view.items() if sent.get(k) != sha and line not in prompt]
    if gone := [k for k in sent if k not in view]:
        live = {e["name"] for e in p._memory_entries()}
        lines += ["## STANDING now reads\n(empty)" if k == STANDING else f"[{k}] retired, ignore"
                  for k in gone if k not in live]
    return ("\n".join([MEMORY_HEAD, *lines]) if lines else ""), {k: v[0] for k, v in view.items()}
