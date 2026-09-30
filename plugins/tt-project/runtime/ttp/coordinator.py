# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The coordinator turn: a short, tool-less, schema-bound decision over a digest of the project.

Prompt layout is cache-friendly: the stable part (role, charter, memory) goes in the system
prompt; the volatile part (state digest + new events) is the user message. The model returns
JSON actions; this module validates them and applies them to the database. Anything that needs
reading files, running commands or thinking hard becomes a task for a worker instead.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import schedule as sched
from .db import SEVERITY_RANK, TERMINAL_TASK_STATES, dependency_ids, load_result
from .project import Project

ACTION_TYPES = ("reply", "task_add", "task_update", "ask_user", "resolve", "notify", "memory_add",
                "charter_update", "schedule_set", "config_set", "noop")

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
            "reversible": {"type": "boolean"}, "recommendation": {"type": "string"},
            "resources": {"type": "array", "items": {"type": "string"}}},
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
    # Hours before an unanswered reversible ask falls back to its recommendation; 0 turns it off.
    "coordinator.ask_timeout_h": float,
}

ASK_DEFAULTS_KEY = "ask_defaults"   # kv: {ask message id: recommendation} for reversible asks
_DEFAULT_NOTE = "\n\nIf there is no answer within "


def system_prompt(p: Project) -> str:
    """Stable across turns so the provider can cache it."""
    role = (p.harness / "prompts" / "coordinator.md").read_text()
    charter = p.charter_path.read_text() if p.charter_path.exists() else "(no charter yet)"
    memory = p.memory_text() or "(no memories yet)"
    from .prompts import restrictions_block
    rules = restrictions_block(p)
    if rules:
        rules += ("\nWorkers are shown this block verbatim; when a spec touches anything it covers, "
                  "restate the relevant restriction in the spec itself.\n\n")
    return f"{rules}{role}\n\n# CHARTER\n{charter}\n\n# MEMORY\n{memory}\n"


def digest(p: Project, gates: dict, event_ids: list[int], msg_ids: list[int]) -> str:
    db = p.db
    now = time.time()
    lines = [f"# STATE at {time.strftime('%Y-%m-%d %H:%M %Z')}",
             "## Project budget (authoritative; your own turn's small spend limit is NOT this budget)"]
    b = p.config()["budget"]
    for prov, g in gates.items():
        n = g.get("numbers", {})
        if g.get("regime") == "windows":
            money = (f"plan windows: {n.get('window')} at {n.get('utilization')}% of the account; "
                     f"the project may use it up to {n.get('limit')}%")
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
        note = (t["blocked_reason"] or str(load_result(t["result"]).get("summary") or ""))[:140]
        lines.append(f"- #{t['id']} | {t['status']} | {t['tier']} | p{t['priority']} | "
                     f"{(now - t['created']) / 3600:.1f}h | {t['title']} | {note}")
    if not rows:
        lines.append("- (none)")
    lines.append("## Recently finished (last 48h)")
    for t in db.q("SELECT * FROM tasks WHERE status IN ('done','failed','cancelled') AND updated>? "
                  "ORDER BY updated DESC LIMIT 15", (now - 172800,)):
        summary = str(load_result(t["result"]).get("summary") or "")[:200]
        lines.append(f"- #{t['id']} {t['status']}: {t['title']} — {summary}")
    lines.append("## Recurring")
    for s in sched.with_costs(db):
        lines.append(f"- {s['name']} ({s['kind']}, every {s['every_s'] // 60} min, "
                     f"{'on' if s['enabled'] else 'off'}, 7d cost ${s['cost_7d']}): {s['description'][:100]}")
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
    lines.append("## Chats attached")
    for c in db.q("SELECT id, label, last_active FROM chats ORDER BY last_active DESC LIMIT 10"):
        lines.append(f"- {c['id']} ({c['label'] or 'chat'}), active {(now - (c['last_active'] or now)) / 60:.0f} min ago")

    lines.append("\n# NEW EVENTS")
    if msg_ids:
        for m in db.q(f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(msg_ids))}) ORDER BY id", msg_ids):
            lines.append(f"- [user message via {m['channel']}, chat={m['chat'] or '-'}] {m['text']}")
    if event_ids:
        for e in db.q(f"SELECT * FROM events WHERE id IN ({','.join('?' * len(event_ids))}) ORDER BY id", event_ids):
            lines.append(f"- [{e['kind']} from {e['source']}, severity {e['severity']}] {e['text'][:1500]}")
    if not msg_ids and not event_ids:
        lines.append("- (none: periodic check — keep work flowing if the charter has unfinished goals)")
    lines.append("\nRespond with the JSON actions object only.")
    return "\n".join(lines)


def _norm_severity(s: str | None) -> str:
    return s if s in SEVERITY_RANK else "normal"


def apply(p: Project, actions: list[dict], default_chat: str | None = None) -> list[str]:
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
                labels = [f"resource:{r}" for r in (a.get("resources") or []) if isinstance(r, str)]
                db.add_task(title, a.get("spec") or "", kind=a.get("kind") or "work", tier=tier,
                            priority=int(a.get("priority") or 3), provider=a.get("provider") or None,
                            budget_usd=float(budget), depends_on=a.get("depends_on") or [],
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
                if a.get("text"):
                    upd["blocked_reason"] = a["text"][:500]
                if a.get("priority"):
                    upd["priority"] = int(a["priority"])
                if a.get("spec"):
                    upd["spec"] = task["spec"] + "\n\n## Update\n" + a["spec"]
                db.update_task(task["id"], **upd)
                if a.get("spec") and task["status"] == "running":
                    stamp = time.strftime("%Y-%m-%d %H:%M")
                    for r in db.q("SELECT dir FROM runs WHERE task=? AND status='running'", (task["id"],)):
                        if r["dir"]:
                            with open(Path(r["dir"], "steer.md"), "a") as f:
                                f.write(f"\n## Update {stamp}\n{a['spec'].strip()}\n")
                if upd.get("status") == "cancelled":
                    for r in db.q("SELECT dir FROM runs WHERE task=? AND status='running'", (task["id"],)):
                        if r["dir"]:
                            Path(r["dir"], "STOP").touch()
            elif t == "ask_user":
                rec = (a.get("recommendation") or "").strip()
                hours = ask_timeout_h(cfg)
                defaults = a.get("reversible") is True and bool(rec) and hours > 0
                text = a["text"].strip()
                if defaults:
                    text += f"{_DEFAULT_NOTE}{hours:g}h, I will go with the recommendation: {rec}"
                with db.tx():
                    mid = db.post("out", text, chat=None, kind="ask",
                                  severity=_norm_severity(a.get("severity") or "high"))
                    if defaults:
                        db.set_kv(ASK_DEFAULTS_KEY, {**db.kv(ASK_DEFAULTS_KEY, {}), str(mid): rec})
                if a.get("reversible") is True and not rec:
                    raise ValueError(f"ask #{mid} is marked reversible without a recommendation, "
                                     f"so it waits for the user")
            elif t == "resolve":
                n = db.x("UPDATE messages SET handled=1 WHERE id=? AND kind='ask'", (int(a["id"]),))
                if not n:
                    raise ValueError(f"no open question #{a.get('id')}")
            elif t == "notify":
                db.post("out", a["text"], chat=None, kind="alert", severity=_norm_severity(a.get("severity")))
            elif t == "memory_add":
                p.add_memory(a["text"], kind=a.get("memory_kind") or "fact", title=a.get("title"))
            elif t == "charter_update":
                section = (a.get("section") or "Notes").strip().title()
                with open(p.charter_path, "a") as f:
                    f.write(f"\n## {section} (added {time.strftime('%Y-%m-%d')})\n{a['text'].strip()}\n")
                p.commit_harness([p.charter_path], f"charter ({section.lower()}): {a['text'].strip()[:80]}")
            elif t == "schedule_set":
                sched.upsert(db, a["name"], a.get("kind") or "llm", a.get("every") or "1d", a.get("at"),
                             bool(a.get("enabled", True)), a.get("budget_usd"), a.get("text") or "",
                             {"spec": a.get("spec") or "", "tier": a.get("tier") or "standard"})
            elif t == "config_set":
                key = a.get("key", "")
                if key not in USER_SETTABLE:
                    raise ValueError(f"{key} is not user-settable from chat")
                p.set_config(key, USER_SETTABLE[key](a.get("value")))
            elif t in ("noop", None):
                pass
            else:
                raise ValueError(f"unknown action {t!r}")
        except Exception as e:   # one bad action is reported back; it never aborts the turn
            problems.append(f"{t}: {e}")
    return problems


def _new_dependencies(db, task: dict, raw: Any) -> list[int]:
    if not isinstance(raw, list):
        raise ValueError(f"#{task['id']} depends_on must be a list of task ids")
    try:
        deps = list(dict.fromkeys(int(d) for d in raw))
    except (TypeError, ValueError):
        raise ValueError(f"#{task['id']} depends_on must be a list of task ids") from None
    for d in deps:
        if d == task["id"]:
            raise ValueError(f"#{task['id']} cannot depend on itself")
        if not db.task(d):
            raise ValueError(f"#{task['id']} depends_on: no task #{d}")
    if db.dependency_cycle(task["id"], deps):
        raise ValueError(f"#{task['id']} depends_on {deps} would create a cycle")
    return deps


def ask_timeout_h(cfg: dict) -> float:
    try:
        return max(0.0, float(cfg["coordinator"].get("ask_timeout_h", 12) or 0))
    except (TypeError, ValueError):
        return 0.0


def expire_asks(p: Project, *, hold: bool = False, now: float | None = None) -> list[int]:
    """Resolve reversible asks left unanswered past the timeout into their recommendation.

    Only asks the coordinator marked reversible with a recommendation are ever listed; anything
    else waits for the user indefinitely. Nothing expires while `hold` is set (the project is at a
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


def open_task_count(p: Project) -> int:
    return len(p.db.q("SELECT id FROM tasks WHERE status NOT IN (%s)" % ",".join("?" * len(TERMINAL_TASK_STATES)),
                      TERMINAL_TASK_STATES))
