# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The coordinator turn: a short, tool-less, schema-bound decision over a digest of the project.

Prompt layout is cache-friendly: the stable part (role, charter, memory) goes in the system
prompt; the volatile part (state digest + new events) is the user message. The model returns
JSON actions; this module validates them and applies them to the database. Anything that needs
reading files, running commands or thinking hard becomes a task for a worker instead.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import schedule as sched
from .db import SEVERITY_RANK, TERMINAL_TASK_STATES, dependency_ids, load_result
from .project import Project
from .runner import stop_runs

ACTION_TYPES = ("reply", "task_add", "task_update", "ask_user", "resolve", "notify", "memory_add",
                "charter_update", "schedule_set", "config_set", "noop")

# Why an ask cannot be decided by the coordinator itself. Anything else is a judgment call.
BLOCKING_REASONS = ("access", "funds", "spend", "review", "merge", "irreversible", "restriction", "human")

ACTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": list(ACTION_TYPES)},
            "chat": {"type": "string"}, "text": {"type": "string"}, "severity": {"type": "string"},
            "title": {"type": "string"}, "spec": {"type": "string"}, "kind": {"type": "string"},
            "tier": {"type": "string"}, "priority": {"type": "integer"}, "provider": {"type": "string"},
            "budget_usd": {"type": "number"}, "depends_on": {"type": "array", "items": {"type": "integer"}},
            "reply_chat": {"type": "string"}, "id": {"type": "integer"}, "status": {"type": "string"},
            "memory_kind": {"type": "string"}, "section": {"type": "string"}, "name": {"type": "string"},
            "every": {"type": "string"}, "at": {"type": "string"}, "enabled": {"type": "boolean"},
            "key": {"type": "string"}, "value": {"type": "string"},
            "blocking": {"type": "string", "enum": list(BLOCKING_REASONS)}, "recommendation": {"type": "string"},
            "resources": {"type": "array", "items": {"type": "string"}}, "exclusive": {"type": "boolean"}},
            "required": ["type"]}},
        "summary": {"type": "string"},
    },
    "required": ["actions"],
}

# Settings the coordinator may change on the user's explicit request. Anything else needs the
# user to edit project.json (or the web app) themselves.
USER_SETTABLE = {
    "budget.daily_usd": float, "budget.weekly_usd": float, "budget.reserve_pct": float,
    "budget.max_parallel_workers": int, "notify.slack": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    "notify.slack_min_severity": str, "notify.chat_min_severity": str, "core_provider": str,
    "coordinator.tier": str, "jev.enabled": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    # Where code tasks branch from: the project's working branch once it has one.
    "delivery.base_ref": str,
    # The runaway valve on task creation; the coordinator may raise it within MAX_TASKS_PER_DAY.
    "coordinator.max_new_tasks_per_day": lambda v: min(int(v), MAX_TASKS_PER_DAY),
    # Skill plugins loaded for this project's workers only (a plan may recommend them).
    "providers.claude.plugin_dirs": lambda v: existing_dirs(dir_list(v)),
    # Hours before an unanswered ask registered with a default falls back to it; 0 turns it off.
    # New asks never get a default, so this only drains asks registered with one.
    "coordinator.ask_timeout_h": float,
}

REJECTED_KEY = "rejected_actions"   # kv: the last turn's rejected actions, shown in the next digest
RECENT_OUT = 5                       # outbound messages the digest repeats, so turns do not resend them
# Digest row lengths. Background rows are cut; new events, asks and open-task notes carry decisions.
NOTE_CHARS = 140
FINISHED_ROWS, FINISHED_CHARS = 10, 120
SENT_CHARS = 100
HANDOFF_KINDS = ("task_done", "task_failed", "task_cancelled", "cancelled_but_done")
MAX_TASKS_PER_DAY = 1000
ASK_DEFAULTS_KEY = "ask_defaults"   # kv: {ask message id: recommendation}; no new ask is added
_DEFAULT_NOTE = "\n\nIf there is no answer within "
# Shown so the user can answer in one word; never applied without their answer.
_REC_NOTE = "\n\nMy recommendation: "


def system_prompt(p: Project) -> str:
    """Stable across turns so the provider can cache it."""
    role = (p.harness / "prompts" / "coordinator.md").read_text()
    charter = p.charter_path.read_text() if p.charter_path.exists() else "(no charter yet)"
    memory = p.memory_text() or "(no memories yet)"
    from .prompts import charter_without_restrictions, restrictions_block
    rules = restrictions_block(p)
    if rules:
        rules += ("\nWorkers are shown this block verbatim; when a spec touches anything it covers, "
                  "restate the relevant restriction in the spec itself.\n\n")
        charter = charter_without_restrictions(charter)
    return f"{rules}{role}\n\n# CHARTER\n{charter}\n\n# MEMORY\n{memory}\n"


def clip(text: Any, n: int) -> str:
    """`text` on one line, at most `n` characters, cut at a word where one is near."""
    s = " ".join(str(text or "").split())
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    space = cut.rfind(" ")
    if space > n * 2 // 3:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def digest(p: Project, gates: dict, event_ids: list[int], msg_ids: list[int]) -> str:
    db = p.db
    now = time.time()
    lines = [f"# STATE at {time.strftime('%Y-%m-%d %H:%M %Z')}",
             "## Project budget (authoritative; your own turn's small spend limit is NOT this budget)"]
    b = p.config()["budget"]
    for prov, g in gates.items():
        n = g.get("numbers", {})
        if g.get("regime") == "windows":
            parts = []
            for w in n.get("pace") or []:
                left = f"{w['hours_left']:.1f} h" if w.get("hours_left") is not None else "unknown time"
                pace = (f"burning {w['burn_per_h']}/h, needs {w['need_per_h']}/h to land at {n.get('limit')}% "
                        f"(on pace for {w['projected']}%)" if w.get("burn_per_h") is not None
                        else "no burn measured yet")
                parts.append(f"{w['window']} {w['utilization']}% used, resets in {left}, {pace}")
            money = ("plan windows (unused capacity is lost at each reset; the target is "
                     f"{n.get('limit')}% by then): " + "; ".join(parts))
            if g.get("level") == "green":
                money += (f" · {n.get('running', 0)} of {g['max_parallel']} worker slots busy: keep enough "
                          f"independent tasks ready to fill the free ones")
        else:
            est = f" (${n['estimated_24h']:.2f} of it estimated)" if n.get("estimated_24h") else ""
            money = (f"project caps (usage-billed providers together): ${n.get('spent_24h', 0):.2f} of "
                     f"${b.get('daily_usd')} per 24h"
                     f"{est}, ${n.get('spent_7d', 0):.2f} of ${b.get('weekly_usd')} per 7 days")
        lines.append(f"- {prov}: {g['level']} ({'; '.join(g['reasons']) or 'ok'}) · {money} · max_tier={g['max_tier']} "
                     f"max_parallel={g['max_parallel']} optional_work={'yes' if g['allow_optional'] else 'no'}")
    lines.append(f"- per-task default budgets: {b.get('task_default_usd')}")
    lines.append("## Open tasks (id | status | tier | priority | age | title | last note)")
    rows = db.q("SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled') ORDER BY priority, id LIMIT 60")
    for t in rows:
        note = clip(t["blocked_reason"] or load_result(t["result"]).get("summary"), NOTE_CHARS)
        lines.append(f"- #{t['id']} | {t['status']} | {t['tier']} | p{t['priority']} | "
                     f"{(now - t['created']) / 3600:.1f}h | {t['title']} | {note}")
    if not rows:
        lines.append("- (none)")
    events = (db.q(f"SELECT * FROM events WHERE id IN ({','.join('?' * len(event_ids))}) ORDER BY id", event_ids)
              if event_ids else [])
    # A task finishing this turn has its full hand-off under NEW EVENTS. Only a hand-off counts:
    # a batch of at most max_events_per_turn can split it from its follow-ups or a retry event.
    in_events = {e["source"] for e in events if e["kind"] in HANDOFF_KINDS}
    finished = db.q("SELECT * FROM tasks WHERE status IN ('done','failed','cancelled') AND updated>? "
                    "ORDER BY updated DESC LIMIT ?", (now - 172800, FINISHED_ROWS))
    if finished:
        lines.append("## Recently finished (last 48h, newest first)")
    for t in finished:
        summary = ("see new events" if f"task:{t['id']}" in in_events
                   else clip(load_result(t["result"]).get("summary"), FINISHED_CHARS))
        lines.append(f"- #{t['id']} {t['status']}: {t['title']} — {summary}")
    recurring = sched.with_costs(db)
    if recurring:
        lines.append("## Recurring")
    for s in recurring:
        lines.append(f"- {s['name']} ({s['kind']}, every {s['every_s'] // 60} min, "
                     f"{'on' if s['enabled'] else 'off'}, 7d cost ${s['cost_7d']}): {clip(s['description'], 100)}")
    blockers = db.q("SELECT * FROM messages WHERE kind='ask' AND handled=0 AND ts>? ORDER BY id DESC LIMIT 10",
                    (now - 14 * 86400,))
    if blockers:
        lines.append("## Open questions to the user (resolve each once answered)")
        pending = p.db.kv(ASK_DEFAULTS_KEY, {})
        hours = ask_timeout_h(p.config())
        for b in blockers:
            if str(b["id"]) in pending and hours > 0:
                left = max(0.0, (b["ts"] + hours * 3600 - now) / 3600)
                when = f"reversible; defaults to its recommendation in {left:.1f}h"
            else:
                when = "waits for the user"
            lines.append(f"- ask #{b['id']} ({when}): {b['text'][:300]}")
    sent = db.q("SELECT * FROM messages WHERE direction='out' AND kind IN ('reply','ask','alert') "
                "ORDER BY id DESC LIMIT ?", (RECENT_OUT,))
    if sent:
        lines.append("## Recently sent to the user (do not repeat these)")
        for m in reversed(sent):
            lines.append(f"- {m['kind']} #{m['id']}, {(now - m['ts']) / 3600:.1f}h ago: {clip(m['text'], SENT_CHARS)}")
    chats = db.q("SELECT id, label, last_active FROM chats ORDER BY last_active DESC LIMIT 10")
    if chats:
        lines.append("## Chats attached")
    for c in chats:
        lines.append(f"- {c['id']} ({c['label'] or 'chat'}), active {(now - (c['last_active'] or now)) / 60:.0f} min ago")

    lines.append("\n# NEW EVENTS")
    if msg_ids:
        for m in db.q(f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(msg_ids))}) ORDER BY id", msg_ids):
            lines.append(f"- [user message via {m['channel']}, chat={m['chat'] or '-'}] {m['text']}")
    for e in events:
        lines.append(f"- [{e['kind']} from {e['source']}, severity {e['severity']}] {e['text'][:1500]}")
    rejected = db.kv(REJECTED_KEY, []) or []
    for x in rejected:
        lines.append(f"- [your previous turn's action was rejected; fix or drop it] {x[:500]}")
    if not msg_ids and not event_ids and not rejected:
        lines.append("- (none: periodic check — keep work flowing if the charter has unfinished goals)")
    lines.append("\nRespond with the JSON actions object only.")
    return "\n".join(lines)


def _norm_severity(s: str | None) -> str:
    return s if s in SEVERITY_RANK else "normal"


# Settings that spend the user's money or eat into their reserve. The coordinator may change them
# only in a turn that carries the user's message: a turn woken by logs, pull requests or a worker's
# hand-off can be steered by text from outside. Everything else it decides on its own.
NEEDS_USER = {"budget.daily_usd", "budget.weekly_usd", "budget.reserve_pct"}


def apply(p: Project, actions: list[dict], default_chat: str | None = None, user_turn: bool = False) -> list[str]:
    """Apply validated actions. Returns human-readable notes about rejected ones, fed back next turn."""
    db, problems = p.db, []
    cfg = p.config()
    for a in actions:
        t = a.get("type")
        try:
            if t == "reply":
                chat = a.get("chat") or default_chat
                db.post("out", a["text"], chat=None if chat in (None, "all") else chat, kind="reply",
                        severity=_norm_severity(a.get("severity") or "normal"))
            elif t == "task_add":
                title = (a.get("title") or "").strip()
                if not title:
                    raise ValueError("task_add needs a title")
                dup = db.one("SELECT id FROM tasks WHERE title=? AND status NOT IN ('done','failed','cancelled')",
                             (title,))
                if dup:
                    raise ValueError(f"duplicate of open task #{dup['id']}")
                cap = int(cfg["coordinator"].get("max_new_tasks_per_day", 40))
                made = db.one("SELECT COUNT(*) n FROM tasks WHERE origin='coordinator' AND created>?",
                              (time.time() - 86400,))["n"]
                if made >= cap:
                    raise ValueError(f"daily cap of {cap} new tasks reached; finish or cancel work first")
                tier = a.get("tier") if a.get("tier") in ("light", "standard", "deep") else "standard"
                budget = a.get("budget_usd") or cfg["budget"]["task_default_usd"].get(tier, 8.0)
                kind_label = "exclusive" if a.get("exclusive") else "resource"
                labels = [f"{kind_label}:{r}" for r in (a.get("resources") or []) if isinstance(r, str)]
                deps = _new_dependencies(db, None, a.get("depends_on") or [])
                # The daemon would block a new task on a dead dependency at once.
                dead = db.dead_dependency(deps)
                if dead:
                    raise ValueError(f"task_add rejected: depends on #{dead[0]} which is {dead[1]}; "
                                     f"drop or replace depends_on")
                db.add_task(title, a.get("spec") or "", kind=a.get("kind") or "work", tier=tier,
                            priority=int(a.get("priority") or 3), provider=a.get("provider") or None,
                            budget_usd=float(budget), depends_on=deps,
                            reply_chat=a.get("reply_chat") or None, origin="coordinator", labels=labels)
            elif t == "task_update":
                task = db.task(int(a["id"]))
                if not task:
                    raise ValueError(f"no task #{a.get('id')}")
                upd: dict[str, Any] = {}
                if a.get("status") in ("queued", "blocked", "cancelled", "done", "waiting"):
                    if task["status"] == "running" and a["status"] != "cancelled":
                        raise ValueError(f"task #{task['id']} is running: send `spec` alone to steer it mid-run, "
                                         f"or set status cancelled")
                    upd["status"] = a["status"]
                    if a["status"] == "queued":
                        upd["blocked_reason"] = None
                if a.get("depends_on") is not None:
                    deps = _new_dependencies(db, task, a["depends_on"])
                    upd["depends_on"] = deps
                    # A task blocked on a dead dependency is released by re-pointing it.
                    if "status" not in upd and task["status"] == "blocked" and \
                            db.dead_dependency(dependency_ids(task)):
                        upd.update(status="queued", blocked_reason=None)
                # The daemon blocks a queued task on a dead dependency at once, so accepting this
                # would report a requeue that does not stick.
                if upd.get("status", task["status"]) == "queued" and ("status" in upd or "depends_on" in upd):
                    dead = db.dead_dependency(upd.get("depends_on", dependency_ids(task)))
                    if dead:
                        dep, why = dead
                        raise ValueError(f"#{task['id']} rejected: depends on #{dep} which "
                                         f"{'does not exist' if why == 'does not exist' else 'is ' + why}; "
                                         f"drop or replace depends_on")
                spec = a.get("spec") or ""
                if a.get("text") and upd.get("status", task["status"]) in ("blocked", "cancelled"):
                    upd["blocked_reason"] = a["text"][:500]
                elif a.get("text"):
                    # Kept as the reason, a note would outlive the state it described.
                    spec = "\n\n".join(x for x in (spec, a["text"]) if x)
                if a.get("priority"):
                    upd["priority"] = int(a["priority"])
                if spec:
                    upd["spec"] = task["spec"] + "\n\n## Update\n" + spec
                db.update_task(task["id"], **upd)
                if spec and task["status"] == "running":
                    stamp = time.strftime("%Y-%m-%d %H:%M")
                    for r in db.q("SELECT dir FROM runs WHERE task=? AND status='running'", (task["id"],)):
                        if r["dir"]:
                            with open(Path(r["dir"], "steer.md"), "a") as f:
                                f.write(f"\n## Update {stamp}\n{spec.strip()}\n")
                if upd.get("status") == "cancelled":
                    stop_runs(db, p.runs, task["id"])
            elif t == "ask_user":
                if a.get("reversible") is True:
                    raise ValueError("ask_user rejected: decide it yourself. A reversible choice is a judgment "
                                     "call: act on it, memory_add a decision and notify at severity low")
                if a.get("blocking") not in BLOCKING_REASONS:
                    raise ValueError(f"ask_user rejected: `blocking` must be one of {', '.join(BLOCKING_REASONS)}; "
                                     f"got {a.get('blocking')!r}. Anything else, decide it yourself")
                text = a["text"].strip()
                for o in db.q("SELECT id, text FROM messages WHERE kind='ask' AND handled=0"):
                    if _same_text(_ask_question(o["text"]), text):
                        raise ValueError(f"already asked as open ask #{o['id']}; it waits for the answer")
                rec = (a.get("recommendation") or "").strip()
                if rec:
                    text += f"{_REC_NOTE}{rec}"
                db.post("out", text, chat=None, kind="ask", severity=_norm_severity(a.get("severity") or "high"))
            elif t == "resolve":
                n = db.x("UPDATE messages SET handled=1 WHERE id=? AND kind='ask'", (int(a["id"]),))
                if not n:
                    raise ValueError(f"no open question #{a.get('id')}")
            elif t == "notify":
                db.post("out", a["text"], chat=None, kind="alert", severity=_norm_severity(a.get("severity")))
            elif t == "memory_add":
                p.add_memory(a["text"], kind=a.get("memory_kind") or "fact", title=a.get("title"))
                if (a.get("memory_kind") or "") == "restriction":
                    _tell_running_workers(db, f"New binding restriction: {a['text'].strip()}")
            elif t == "charter_update":
                section = (a.get("section") or "Notes").strip().title()
                with open(p.charter_path, "a") as f:
                    f.write(f"\n## {section} (added {time.strftime('%Y-%m-%d')})\n{a['text'].strip()}\n")
                p.commit_harness([p.charter_path], f"charter ({section.lower()}): {a['text'].strip()[:80]}")
                if section.startswith("Restriction"):
                    _tell_running_workers(db, f"New binding restriction: {a['text'].strip()}")
            elif t == "schedule_set":
                sched.upsert(db, a["name"], a.get("kind") or "llm", a.get("every") or "1d", a.get("at"),
                             bool(a.get("enabled", True)), a.get("budget_usd"), a.get("text") or "",
                             {"spec": a.get("spec") or "", "tier": a.get("tier") or "standard"})
            elif t == "config_set":
                key = a.get("key", "")
                if key not in USER_SETTABLE:
                    raise ValueError(f"{key} is not user-settable from chat")
                if key in NEEDS_USER and not user_turn:
                    raise ValueError(f"{key} needs the user's approval: ask_user (blocking spend) with the exact value, and set "
                                     f"it in the turn that carries their yes")
                p.set_config(key, USER_SETTABLE[key](a.get("value")))
            elif t in ("noop", None):
                pass
            else:
                raise ValueError(f"unknown action {t!r}")
        except Exception as e:   # one bad action is reported back; it never aborts the turn
            problems.append(f"{t}: {e}")
    return problems


def _ask_question(text: str) -> str:
    return text.split(_DEFAULT_NOTE)[0].split(_REC_NOTE)[0]


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def _new_dependencies(db, task: dict | None, raw: Any) -> list[int]:
    """Validated dependency ids for `task`, or for a task not yet created when `task` is None."""
    who = f"#{task['id']}" if task else "task_add"
    if not isinstance(raw, list):
        raise ValueError(f"{who} depends_on must be a list of task ids")
    try:
        deps = list(dict.fromkeys(int(d) for d in raw))
    except (TypeError, ValueError):
        raise ValueError(f"{who} depends_on must be a list of task ids") from None
    for d in deps:
        if task and d == task["id"]:
            raise ValueError(f"{who} cannot depend on itself")
        if not db.task(d):
            raise ValueError(f"{who} depends_on: no task #{d}")
    # Nothing depends on a new task yet, so only an existing one can close a loop.
    if task and db.dependency_cycle(task["id"], deps):
        raise ValueError(f"{who} depends_on {deps} would create a cycle")
    return deps


def dir_list(v: Any) -> list[str]:
    """Directories from a config value. The action schema carries `value` as a string, so a list
    arrives JSON-encoded or comma/newline separated, and an older config may hold such a string
    as a single list element."""
    out: list[str] = []
    for item in (v if isinstance(v, list) else [v]):
        if isinstance(item, list):
            out += dir_list(item)
            continue
        text = str(item or "").strip()
        if text.startswith("["):
            try:
                out += dir_list(json.loads(text))
                continue
            except ValueError:
                pass
        out += [x.strip() for x in re.split(r"[,\n]", text) if x.strip()]
    return out


def existing_dirs(dirs: list[str]) -> list[str]:
    missing = [d for d in dirs if not Path(os.path.expanduser(d)).is_dir()]
    if missing:
        raise ValueError(f"not a directory on this machine: {', '.join(missing)}; nothing was changed")
    return dirs


def ask_timeout_h(cfg: dict) -> float:
    try:
        return max(0.0, float(cfg["coordinator"].get("ask_timeout_h", 1) or 0))
    except (TypeError, ValueError):
        return 0.0


def expire_asks(p: Project, *, hold: bool = False, now: float | None = None) -> list[int]:
    """Resolve asks registered with a default and left unanswered past the timeout into it.

    New asks are never registered, so this only drains asks that got a default when they were
    asked; every other ask waits for the user indefinitely. Nothing expires while `hold` is set (the project is at a
    cap) or while a user message is unhandled, since that message may be the answer. A due ask
    with any user message after it, handled or not, may have been answered without a `resolve`:
    it keeps waiting for the user and the coordinator is asked to confirm instead. The user is
    told what was decided and the coordinator gets an event to act on it. Returns expired ask ids.
    """
    db = p.db
    if not db.kv(ASK_DEFAULTS_KEY, {}):
        return []
    now = now or time.time()
    cfg = p.config()
    hours = ask_timeout_h(cfg)
    with db.tx():   # re-read inside: a resolve or a new ask may land between ticks
        pending = db.kv(ASK_DEFAULTS_KEY, {})
        open_asks = {str(r["id"]): r for r in db.q(
            f"SELECT * FROM messages WHERE kind='ask' AND handled=0 AND id IN ({','.join('?' * len(pending))})",
            [int(k) for k in pending])}
        live = {k: v for k, v in pending.items() if k in open_asks}
        due = [k for k in live if hours > 0 and open_asks[k]["ts"] + hours * 3600 <= now]
        if hold or (due and db.one("SELECT id FROM messages WHERE direction='in' AND handled=0")):
            due = []
        for k in [k for k in due if db.one("SELECT id FROM messages WHERE direction='in' AND id>?", (int(k),))]:
            due.remove(k)
            live.pop(k)
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (now, "daemon", "ask_timeout", "normal",
                  f"Ask #{k} reached its {hours:g}h timeout, but the user wrote after it was asked, so its "
                  f"recommendation was NOT applied. If the user answered it, act on the answer and `resolve` "
                  f"it; otherwise it now waits for the user. The question was: "
                  f"{open_asks[k]['text'].split(_DEFAULT_NOTE)[0][:300]}", "queued"))
        for k in due:
            ask, rec = open_asks[k], live.pop(k)
            # at least high, and never under the chat floor: the user must see what was decided for them
            severity = max((ask["severity"], "high", cfg["notify"].get("chat_min_severity")),
                           key=lambda n: SEVERITY_RANK.get(n, -1))
            db.x("UPDATE messages SET handled=1 WHERE id=?", (ask["id"],))
            question = ask["text"].split(_DEFAULT_NOTE)[0][:300]
            db.post("out", f"No answer to ask #{k} after {hours:g}h, so I went with the recommendation: {rec}\n"
                           f"It can be reversed: reply to change it.\nThe question was: {question}",
                    chat=None, kind="alert", severity=severity)
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (now, "daemon", "ask_timeout", "normal",
                  f"Ask #{k} got no answer in {hours:g}h. The user was told its recommendation now applies: "
                  f"{rec}. Act on it; it is not permission for anything beyond that choice. "
                  f"The question was: {question}", "queued"))
        if live != pending:
            db.set_kv(ASK_DEFAULTS_KEY, live)
    return [int(k) for k in due]


def _tell_running_workers(db, text: str) -> None:
    """A new restriction binds work already in flight, not only work started later: it goes to
    every running worker's update file, which the worker receives mid-run."""
    stamp = time.strftime("%Y-%m-%d %H:%M")
    for r in db.q("SELECT dir FROM runs WHERE status='running' AND role!='coordinator'"):
        if r["dir"] and Path(r["dir"]).is_dir():
            with open(Path(r["dir"], "steer.md"), "a") as f:
                f.write(f"\n## Update {stamp}\n{text}\n")


def open_task_count(p: Project) -> int:
    return len(p.db.q("SELECT id FROM tasks WHERE status NOT IN (%s)" % ",".join("?" * len(TERMINAL_TASK_STATES)),
                      TERMINAL_TASK_STATES))
