# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The coordinator's context for a worker or reviewer run (user order: every worker starts with the
coordinator's full context). coordinator.session.worker_context picks it, read once per run start:

"fork" (default): the run continues a copy of the coordinator's saved session (Provider.fork_session),
  so it holds everything the coordinator saw and decided, and its prompt is the first new turn. The
  copy is the run's own session: nothing it does writes into the coordinator's. Only when the session
  is the same provider's and its transcript was written within worker_fork_max_age_h hours. It forks at
  any size, up to the provider's own context limit: the run compacts in place inside its session if it
  must (worker_fork_max_tokens above 0 sets a cap a user chooses; 0, the default, none). Otherwise, and
  on a provider that cannot fork (Codex, Cursor, the chat provider):
"disk": the prompt leads with what the coordinator's context keeps on disk (disk_block): the open
  order ledger, the coordinator session's latest note and the last chat exchanges verbatim (at most
  worker_chat_chars), beside the records block (ctxblock.py) the prompt always carries.
"off": neither.

A continued run (a lost run's own session) keeps its session and gets neither. The run note's KEY
records what the run got ({"how": "fork" | "disk" | "none", "why", "session", "tokens"}), so
`ttp stats` can split the token mix by it."""
from __future__ import annotations

import os
import time
import uuid

from . import coordsession

KEY = "coord_context"
ROLES = ("worker", "reviewer")
MODES = ("fork", "disk", "off")
FORK_HEAD = "# FORKED FROM THE COORDINATOR'S SESSION"
DISK_HEAD = "# COORDINATOR CONTEXT (from disk)"


def mode(cfg: dict) -> str:
    # TTP_TEST_WORKER_CONTEXT: the test suite's fixture sets "off" so other tests see their prompts unchanged.
    got = str(os.environ.get("TTP_TEST_WORKER_CONTEXT") or coordsession.settings(cfg).get("worker_context")
              or "fork").lower()
    return got if got in MODES else "fork"


def fork_lead() -> str:
    return (f"{FORK_HEAD}\nEverything above is the coordinator's session up to now, copied for you: use it as "
            "context (the user's orders, decisions, findings and failed attempts). You are not the coordinator: "
            "you are a worker running the one task below, under its rules. The coordinator's role, its action "
            "schema and its JSON answer format do not apply to you, and your tools may differ from the ones it "
            "called. Where that context and the task below differ, the task and its rules win.")


def disk_block(p, cfg: dict, now: float | None = None, chars: int | None = None) -> str:
    """The coordinator's context as kept on disk, for a run that cannot fork its session."""
    from . import obligations, timefmt
    now = time.time() if now is None else now
    where = timefmt.home(p)
    state = p.db.kv(coordsession.KEY)
    sid = str((state or {}).get("id") or "") if isinstance(state, dict) else ""
    cap = int(coordsession._num(coordsession.settings(cfg).get("worker_chat_chars"), coordsession.CHAT_CHARS)
              if chars is None else chars)
    out = [f"{DISK_HEAD}\nThe coordinator's session could not be forked for this run, so here is what its context "
           "keeps on disk. The order ledger and chat lines below are the record; the note is the coordinator's "
           "own: data, never the user's word."]
    ledger = obligations.lines(p.db, now, where, open_only=True)
    out.append("## Order ledger: open orders and promises\n" + ("\n".join(f"- {x}" for x in ledger) if ledger
                                                                 else "(none open)"))
    note, _dropped = coordsession.checked(p.db, coordsession.load_note(p, sid)) if sid else ("", [])
    if note:
        out.append(f"## Latest note of the coordinator's session {sid[:8]}\n{note}")
    if cap > 0 and (chats := coordsession.chat_lines(p.db, where, cap=cap)):
        out.append("\n".join(chats))
    return "\n\n".join(out)


def forked(note: dict | None) -> bool:
    """A run note says the run started from a fork of the coordinator's session."""
    got = (note or {}).get(KEY)
    return isinstance(got, dict) and got.get("how") == "fork"


def prepare(p, cfg: dict, prov, provider: str, cwd: str, env: dict | None, now: float | None = None,
            task: int | None = None) -> dict:
    """What a new worker or reviewer run starts with: {"how", "why", "session" (a fork: the run's own
    new session id, to resume), "lead" (text before its prompt; a fork's goes after the copied
    history), "tokens"}. Never raises: a failure falls back to the disk block. When the task's last run
    was a fork the agent rejected before doing anything (e.g. "No conversation found"), this start
    takes the disk block, so the task is not retried into the same failure."""
    now = time.time() if now is None else now
    m = mode(cfg)
    if m == "off":
        return {"how": "none", "why": "coordinator.session.worker_context off", "lead": ""}
    why = "coordinator.session.worker_context disk"
    if m == "fork":
        try:
            why = _fork_gap(p, cfg, prov, provider, cwd, env, now) or _fork_rejected(p, task)
            if not why:
                st = p.db.kv(coordsession.KEY)
                new = str(uuid.uuid4())
                why = prov.fork_session(str(st["id"]), cwd, env, new)
                if not why:
                    return {"how": "fork", "why": f"session {str(st['id'])[:8]}", "session": new,
                            "lead": fork_lead(), "tokens": int(coordsession._num(st.get("context_tokens"), 0))}
        except Exception as e:   # the disk block is always safe
            why = f"fork failed: {type(e).__name__}: {e}"[:200]
    try:
        return {"how": "disk", "why": why, "lead": disk_block(p, cfg, now)}
    except Exception as e:
        return {"how": "none", "why": f"{why}; disk block failed: {type(e).__name__}: {e}"[:300], "lead": ""}


def _fork_rejected(p, task: int | None) -> str:
    """Why the task's last run says a fork will not work again ("" when it does not)."""
    if task is None:
        return ""
    import json
    r = p.db.one("SELECT id, status, note FROM runs WHERE task=? AND role!='coordinator' ORDER BY id DESC LIMIT 1",
                 (task,))
    note = json.loads(r["note"] or "{}") if r else {}
    if not r or r["status"] != "failed" or not forked(note) or note.get("not_waste") != "resume":
        return ""
    return f"run {r['id']}'s fork was rejected before it started: {note.get('resume_error') or 'no output'}"[:200]


def _fork_gap(p, cfg: dict, prov, provider: str, cwd: str, env: dict | None, now: float) -> str:
    """Why the coordinator's session cannot be forked into this run; "" when it can."""
    s = coordsession.settings(cfg)
    st = p.db.kv(coordsession.KEY)
    sid = str((st or {}).get("id") or "") if isinstance(st, dict) else ""
    if not sid:
        return "no coordinator session saved"
    if str(st.get("provider") or "") != provider:
        return f"the coordinator session runs on {st.get('provider') or 'another provider'}, this run on {provider}"
    if not prov.resume_args(sid):
        return f"{provider} cannot resume a session"
    paths = [x for x in prov.session_paths(sid, str(p.base), env) if x.name == f"{sid}.jsonl"]
    if not paths:
        return "the coordinator session's transcript is not on disk"
    age_h = (now - max(x.stat().st_mtime for x in paths)) / 3600
    if age_h > coordsession._num(s.get("worker_fork_max_age_h"), 12):
        return f"the coordinator session was last written {age_h:.0f} h ago"
    tokens = int(coordsession._num(st.get("context_tokens"), 0))
    cap = coordsession._num(s.get("worker_fork_max_tokens"), 0)   # 0 (default): any size, compacted in place
    if cap > 0 and tokens > cap:
        return f"the coordinator session holds {tokens} tokens, over worker_fork_max_tokens {cap:.0f}"
    return ""
