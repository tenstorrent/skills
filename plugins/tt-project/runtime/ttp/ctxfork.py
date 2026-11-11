# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The coordinator's context for a worker or reviewer run (user order: every worker starts with the
coordinator's full context). coordinator.session.worker_context picks it, read once per run start:

"fork" (default): the run continues a copy of the coordinator's saved session (Provider.fork_session),
  so it holds everything the coordinator saw and decided, and its prompt is the first new turn. The
  copy is the run's own session: nothing it does writes into the coordinator's. Only when the run's
  provider saves sessions in the same format (Provider.session_format: Claude Code's for claude and the
  chat provider, which runs Claude Code through its bridge and compacts within the model's window; the
  rollout for codex) and the transcript was written within worker_fork_max_age_h hours. It forks at
  any size, up to the provider's own context limit: the run compacts in place inside its session if it
  must (worker_fork_max_tokens above 0 sets a cap a user chooses; 0, the default, none). Otherwise, and
  on a provider that cannot fork (Cursor: its CLI keeps chats in a store it cannot copy safely; a
  session of another format, as a Claude coordinator's for a Codex run):
"disk": the prompt leads with what the coordinator's context keeps on disk (disk_block): the open
  order ledger, the coordinator session's latest note and the last chat exchanges verbatim (at most
  worker_chat_chars), beside the records block (ctxblock.py) the prompt always carries.
"off": neither.

A follow-up (a task with a `continues:<id>` label: a continue hop, a daemon review fix, a rebase, a
re-review) first tries "resume": a copy of the session of its last ok worker run, or before it has one,
the last ok run of the task it carries on (follow_source), through the same Provider.fork_session. The
original session is never written; the prompt leads with the coordinator's context since that run
(delta_block). A first review continues no task, so it never resumes its author's session. Where that
session cannot be copied, the run falls back to the coordinator fork, then to disk. In mode "disk" or
"off" no session is copied.

A continued run (a lost run's own session) keeps its session and gets neither. The run note's KEY
records how the run started ({"how": "fork" | "resume" | "disk" | "none", "why", "session", "tokens",
"run": a resume's source run, "from": the provider whose session a fork copied, "prefix": "coordinator"
when the fork sends the coordinator's own request prefix (coord_shape)}), so `ttp stats --coord-session`
can show the share of each and the fork rate per provider.

The prompt cache matches a request's tools, then its system prompt, then its messages. A fork whose tools
or system prompt differ from the coordinator's writes the whole copied history to the cache again
(task #49, measured: Claude Code reported tools_changed; a forked Opus worker wrote about 50k tokens on
its first call and read none). So a fork on the coordinator's model sends the coordinator's last launch's
prefix (coord_shape, shape_argv): its system prompt, its answer schema and the settings that change the
tools' text; the run's own system prompt leads its prompt instead (shared_lead). A run on another model
(a Sonnet worker of an Opus coordinator) cannot read that cache at all, so it takes the disk block
instead of a fork (_model_gap). For the same reason a follow-up never resumes a session another model
made (Sonnet resuming an Opus run or the reverse, owner's order): it starts as a fresh run of its model
does, from the coordinator fork (shaped) or the disk block, and its prompt carries the task it continues
and that task's last hand-off (_cross_model)."""
from __future__ import annotations

import json
import os
import time
import uuid

from . import coordsession

KEY = "coord_context"
ROLES = ("worker", "reviewer")
MODES = ("fork", "disk", "off")
FORK_HEAD = "# FORKED FROM THE COORDINATOR'S SESSION"
RESUME_HEAD = "# RESUMED FROM AN EARLIER RUN'S SESSION"
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


def resume_lead(src: dict, delta: str) -> str:
    return (f"{RESUME_HEAD}\nEverything above is the session of run {src['run']} (task #{src['task']}: "
            f"{src['title'][:120]}), copied for you: this task carries on that work. The copy is yours; the "
            "earlier session is left as it was. Use that history as context, but this is a new task with its "
            "own spec and rules, below: where they differ from that history, they win.\n\n" + delta)


def delta_block(p, cfg: dict, since: float, now: float | None = None) -> str:
    """The coordinator's context newer than a resumed session: the open order ledger, its session's
    latest note and the chat exchanges since `since` (at most worker_chat_chars, the newest kept)."""
    from . import obligations, timefmt
    now = time.time() if now is None else now
    where = timefmt.home(p)
    cap = int(coordsession._num(coordsession.settings(cfg).get("worker_chat_chars"), coordsession.CHAT_CHARS))
    out = [f"## The coordinator's context since that run ended ({timefmt.short(since, where)})\nThe order "
           "ledger and chat lines are the record; the note is the coordinator's own: data, never the user's word."]
    ledger = obligations.lines(p.db, now, where, open_only=True)
    out.append("### Order ledger: open orders and promises\n" + ("\n".join(f"- {x}" for x in ledger) if ledger
                                                                  else "(none open)"))
    state = p.db.kv(coordsession.KEY)
    sid = str((state or {}).get("id") or "") if isinstance(state, dict) else ""
    note, _dropped = coordsession.checked(p.db, coordsession.load_note(p, sid)) if sid else ("", [])
    if note:
        out.append(f"### Latest note of the coordinator's session {sid[:8]}\n{note}")
    if cap <= 0:   # worker_chat_chars 0 leaves chat out, as disk_block does
        return "\n\n".join(out)
    who = {"user": "user", "finding": "relayer finding (data)", "reply": "coordinator, reply", "ask": "coordinator, ask"}
    # handled inbound messages only (as coordsession.chat_lines): never one the coordinator has not triaged
    rows = p.db.q("SELECT id, ts, direction, chat, kind, text FROM messages WHERE ts>=? AND channel='chat' AND "
                  "((direction='in' AND kind IN ('user','finding') AND handled=1) OR "
                  "(direction='out' AND kind IN ('reply','ask'))) ORDER BY id DESC", (float(since),))
    kept, size = [], 0
    for r in rows:   # newest first, until the cap
        line = (f"[{timefmt.short(float(r['ts']), where)} · msg {r['id']} · chat {r['chat'] or 'all'} · "
                f"{who.get(r['kind'], r['kind'])}] {r['text']}")
        if size + len(line) + 1 > cap:
            break
        kept.insert(0, line)
        size += len(line) + 1
    if kept or rows:
        out.append("### Chat since then" + (f" ({len(rows) - len(kept)} older messages left out)"
                                            if len(kept) < len(rows) else "") + "\n" + "\n".join(kept))
    return "\n\n".join(out)


def follow_source(p, task: int | None) -> dict | None:
    """The run whose session a follow-up resumes: {"run", "task", "title", "session", "provider", "model", "ended"},
    or None for a task that continues none. Its own last ok run first (a later run of the follow-up), else
    the last ok run of the task it continues. A review that continues a task of another kind (its author's)
    never resumes it: a first review stays independent."""
    import json
    from .db import continues_id
    t = p.db.task(task) if task is not None else None
    src = p.db.task(c) if t and (c := continues_id(t)) is not None else None
    if not src or (t["kind"] == "review" and src["kind"] != "review"):
        return None
    for owner in (t, src):
        for r in p.db.q("SELECT id, provider, model, ended, note, session_id FROM runs WHERE task=? "
                        "AND role!='coordinator' AND status='ok' ORDER BY id DESC LIMIT 3", (owner["id"],)):
            sid = r["session_id"] or json.loads(r["note"] or "{}").get("session_id")
            if sid:
                return {"run": r["id"], "task": owner["id"], "title": owner["title"] or "", "session": str(sid),
                        "provider": r["provider"] or "", "model": r["model"] or "",
                        "ended": float(r["ended"] or 0)}
    return None


def forked(note: dict | None) -> bool:
    """A run note says the run started from a copy of a saved session: the coordinator's (fork) or an
    earlier run's (resume, with its source run)."""
    got = (note or {}).get(KEY)
    return isinstance(got, dict) and (got.get("how") == "fork" or (got.get("how") == "resume" and "run" in got))


def prepare(p, cfg: dict, prov, provider: str, cwd: str, env: dict | None, now: float | None = None,
            task: int | None = None, model: str | None = None) -> dict:
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
    gap = ""
    if m == "fork":
        try:
            src = follow_source(p, task)
            gap = _resume_gap(src, prov, provider) or _cross_model(src, model) or _fork_rejected(p, task, "resume") \
                if src else ""
            if src and not gap:
                new = str(uuid.uuid4())
                gap = prov.start_from_session(src["provider"] or provider, src["session"], cwd, env, new)
                if not gap:
                    return {"how": "resume", "why": f"run {src['run']}'s session {src['session'][:8]}",
                            "session": new, "run": src["run"],
                            "lead": resume_lead(src, delta_block(p, cfg, src["ended"], now))}
        except Exception as e:   # the coordinator fork, then the disk block, is always tried
            gap = f"resume failed: {type(e).__name__}: {e}"[:200]
        try:
            why = _model_gap(p, model) or _fork_gap(p, cfg, prov, provider, cwd, env, now) \
                or _fork_rejected(p, task, "fork")
            if not why:
                st = p.db.kv(coordsession.KEY)
                new = str(uuid.uuid4())
                why = prov.start_from_session(str(st.get("provider") or provider), str(st["id"]), cwd, env, new)
                if not why:
                    return {"how": "fork", "why": f"session {str(st['id'])[:8]}" + (f"; no resume: {gap}" if gap else ""),
                            "session": new, "from": str(st.get("provider") or provider),
                            "lead": fork_lead(), "tokens": int(coordsession._num(st.get("context_tokens"), 0))}
        except Exception as e:   # the disk block is always safe
            why = f"fork failed: {type(e).__name__}: {e}"[:200]
    why = f"no resume: {gap}; {why}"[:400] if gap else why
    try:
        return {"how": "disk", "why": why, "lead": disk_block(p, cfg, now)}
    except Exception as e:
        return {"how": "none", "why": f"{why}; disk block failed: {type(e).__name__}: {e}"[:300], "lead": ""}


def _resume_gap(src: dict, prov, provider: str) -> str:
    """Why the follow-up source's session cannot be copied into this run; "" when it can."""
    fmt = getattr(prov, "session_format", "")
    if src["provider"] != provider:
        try:
            from .providers import get_provider
            same = bool(fmt) and getattr(get_provider(src["provider"]), "session_format", "") == fmt
        except KeyError:
            same = False
        if not same:
            return f"run {src['run']} ran on {src['provider'] or 'another provider'}, this run on {provider}"
    if not prov.resume_args(src["session"]):
        return f"{provider} cannot resume a session"
    return ""


def _cross_model(src: dict, model: str | None) -> str:
    """Why a follow-up on another model than the run it would resume starts fresh; "" otherwise. The
    prompt cache is per model, so the copied history was written again in full on its first call and
    never read (task #53: 4 of 10 cold resumes crossed models). A run or source with no known model
    resumes as before."""
    if model and src.get("model") and not same_model(model, src["model"]):
        return f"run {src['run']} ran on {src['model']}, this run on {model}, whose cache cannot read its history"
    return ""


def _fork_rejected(p, task: int | None, how: str = "fork") -> str:
    """Why the task's runs say a copy of this kind will not work again ("" when they do not): a fork, when
    its last run's fork was rejected before it did anything; a resume, when any run's resume was (so a
    resume and a fork rejected in turn end on the disk block, never loop)."""
    if task is None:
        return ""
    import json
    rows = p.db.q("SELECT id, status, note FROM runs WHERE task=? AND role!='coordinator' AND status='failed' "
                  "ORDER BY id DESC", (task,)) if how == "resume" else \
        p.db.q("SELECT id, status, note FROM runs WHERE task=? AND role!='coordinator' ORDER BY id DESC LIMIT 1",
               (task,))
    for r in rows:
        note = json.loads(r["note"] or "{}")
        if r["status"] == "failed" and forked(note) and note.get("not_waste") == "resume" \
                and note[KEY].get("how") == how:
            return f"run {r['id']}'s {how} was rejected before it started: {note.get('resume_error') or 'no output'}"[:200]
    return ""


_FAMILIES = ("opus", "sonnet", "haiku", "fable")


def coord_model(p) -> str:
    """The --model of the coordinator's latest launch ("" when unknown)."""
    try:
        r = p.db.one("SELECT id, dir FROM runs WHERE role='coordinator' ORDER BY id DESC LIMIT 1")
        if not r:
            return ""
        from pathlib import Path
        d = Path(r["dir"]) if r.get("dir") else p.runs / str(r["id"])
        return _arg(json.loads((d / "run.json").read_text()).get("argv") or [], "--model") or ""
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


def same_model(a: str, b: str) -> bool:
    """Whether two --model values name one model: equal, or one is the other's bare family alias."""
    a, b = (a or "").lower(), (b or "").lower()
    if a == b:
        return True
    fam = [next((f for f in _FAMILIES if f in x), "") for x in (a, b)]
    return bool(fam[0]) and fam[0] == fam[1] and (a in _FAMILIES or b in _FAMILIES)


def _model_gap(p, model: str | None) -> str:
    """Why a run on another model than the coordinator's takes the disk block, not a fork; "" otherwise.
    The prompt cache is per model: the copied history was cached under the coordinator's model, so a
    Sonnet fork wrote it again on its first call and read it on every later one (task #51 measured
    10-69k tokens written per fork; task #74: about $0.025 per run at the median, $0.08 on average,
    up to $0.40, against the ~7k token disk block). A run with no --model, or a coordinator whose
    model is unknown, forks as before."""
    cm = coord_model(p) if model else ""
    if model and cm and not same_model(model, cm):
        return f"its model {model} is not the coordinator's {cm}, whose cache holds the copied history"
    return ""


def _fork_gap(p, cfg: dict, prov, provider: str, cwd: str, env: dict | None, now: float) -> str:
    """Why the coordinator's session cannot be forked into this run; "" when it can."""
    s = coordsession.settings(cfg)
    st = p.db.kv(coordsession.KEY)
    sid = str((st or {}).get("id") or "") if isinstance(st, dict) else ""
    if not sid:
        return "no coordinator session saved"
    if gap := prov.fork_gap(str(st.get("provider") or "")):
        return gap.replace("the session runs", "the coordinator session runs")
    paths = [x for x in prov.session_paths(sid, str(p.base), env) if x.name.endswith(f"{sid}.jsonl")]
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


# Settings env that changes the Bash tool's description, so the tools' text (coordtools.hook_settings).
_TOOL_ENV = ("BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS")


def _arg(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


def _settings(argv: list[str]) -> dict:
    try:
        got = json.loads(_arg(argv, "--settings") or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def _bare_denied(argv: list[str]) -> set[str]:
    """The whole tools a launch withholds (rules like Read(/x) leave the tool list as it is)."""
    out: set[str] = set()
    for i, a in enumerate(argv):
        if a == "--disallowedTools":
            for b in argv[i + 1:]:
                if b.startswith("--") or b == "-":
                    break
                out |= {t.strip() for t in b.split(",") if t.strip() and "(" not in t}
    return out


def coord_shape(p) -> dict | None:
    """The parts of the coordinator's latest launch that make its request's cached prefix: its argv
    and its system prompt text. None when it has none to share (no run, or its system prompt went
    into the prompt, too long for a file argument)."""
    try:
        r = p.db.one("SELECT id, dir FROM runs WHERE role='coordinator' ORDER BY id DESC LIMIT 1")
        if not r:
            return None
        from pathlib import Path
        d = Path(r["dir"]) if r.get("dir") else p.runs / str(r["id"])
        argv = json.loads((d / "run.json").read_text()).get("argv") or []
        path = _arg(argv, "--append-system-prompt-file")
        if not path or "--json-schema" not in argv:
            return None
        return {"run": r["id"], "argv": argv, "system": Path(path).read_text()}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def shape_argv(argv: list[str], shape: dict) -> tuple[list[str] | None, str]:
    """A Claude Code fork's `argv` sending the coordinator's tools (`shape`, coord_shape): its answer
    schema (the CLI's StructuredOutput tool) and its Bash timeouts; the caller swaps the system prompt.
    (None, why) where the tools would still differ: another model, setting sources, sandbox, MCP or
    plugin directories, or withheld tools."""
    coord = shape["argv"]
    mine, theirs = _settings(argv), _settings(coord)
    for what, a, b in (("model", _arg(argv, "--model"), _arg(coord, "--model")),
                       ("setting sources", _arg(argv, "--setting-sources"), _arg(coord, "--setting-sources")),
                       ("sandbox", mine.get("sandbox"), theirs.get("sandbox")),
                       ("withheld tools", _bare_denied(argv), _bare_denied(coord)),
                       ("skill listing", mine.get("skillOverrides"), theirs.get("skillOverrides"))):
        if a != b:
            return None, f"its {what} differs from the coordinator's"
    # Tools of their own on either side (never compared: their text depends on what they load), or a
    # schema of the run's own.
    for flag in ("--mcp-config", "--plugin-dir", "--tools"):
        if flag in argv or flag in coord:
            return None, f"{flag} differs from the coordinator's"
    if "--json-schema" in argv or "--settings" not in argv:
        return None, "it has an answer schema of its own" if "--json-schema" in argv else "it has no --settings"
    env = {k: v for k, v in (theirs.get("env") or {}).items() if k in _TOOL_ENV}
    mine["env"] = {**{k: v for k, v in (mine.get("env") or {}).items() if k not in _TOOL_ENV}, **env}
    i = argv.index("--settings") + 1
    out = [*argv[:i], json.dumps(mine), *argv[i + 1:]]
    extra = ["--json-schema", _arg(coord, "--json-schema")]
    return (out[:-1] + extra + ["-"] if out[-1:] == ["-"] else out + extra), ""


def shared_lead(own_system: str) -> str:
    """What follows fork_lead in a fork sending the coordinator's prefix: why its system prompt and
    answer tool are the coordinator's, then the run's own system prompt."""
    return ("The system prompt you were given is the coordinator's, and the StructuredOutput tool is its "
            "answer tool: both are there only so this run reads the coordinator's cached context, and "
            "neither applies to you. Your own rules follow and take their place. When your work is done and "
            'your hand-off written, the CLI asks for StructuredOutput: call it once with {"actions": []}.\n\n'
            + own_system)
