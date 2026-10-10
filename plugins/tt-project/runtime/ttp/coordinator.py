# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The coordinator turn: a short, tool-less, schema-bound decision over a digest of the project.

Prompt layout is cache-friendly, most stable first: restrictions and role, then charter and the
memory snapshot, then the volatile part (state digest + new events) as the user message. Where the
provider can mark a cache breakpoint inside the user message, charter and memory lead it as a block
of their own, so editing them does not re-pay the role prompt; elsewhere they end the system prompt. The model returns
JSON actions; this module validates them and applies them to the database. Anything that needs
reading files, running commands or thinking hard becomes a task for a worker instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import locks
from . import anchors, effort, ends, heal, jevuse, machine_ledger, machines, pauseends, prguard, push, reviewcap, shared, unblock, upstream, worktree
from . import screen as scr
from . import schedule as sched
from .db import (PAUSED_RESOURCES_KEY, SEVERITY_RANK, SHARED_SEEN_KEY, TERMINAL_TASK_STATES, continues_id, deferral,
                 dependency_ids, dump_result, host_line, load_result, task_outcome, without_deferral)
from .project import (COORDINATOR_MEMORY_CHARS, WORKER_MEMORY_CHARS, Project, code_tasks_may_push, durable_append,
                      durable_write, push_queue_number, push_queue_on)
from .runner import stop_runs

ACTION_TYPES = ("reply", "task_add", "task_update", "ask_user", "resolve", "notify", "memory_add", "memory_forget",
                "charter_update", "schedule_set", "config_set", "resource_pause", "observation_mute", "pr_approve",
                "escalate", "noop")

# A deferred task's `start_after`: `now`, a delay (`90m`, `3d`) or an ISO date or time (local
# unless it names a zone). Plain character classes, so every provider's schema engine takes it.
START_AFTER_RE = (r"^(now|[0-9]+ ?[smhdw]|[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9]{2}:[0-9]{2}(:[0-9]{2})?)?"
                  r"(Z|[+-][0-9]{2}:?[0-9]{2})?)$")
START_WHEN_CHARS = 1000
START_WHY_CHARS = 200     # the plain words for a start_when probe, shown to the user instead
MAX_DEFER_S = 365 * 86400

# Why an ask cannot be decided by the coordinator itself. Anything else is a judgment call.
BLOCKING_REASONS = ("access", "funds", "spend", "review", "merge", "irreversible", "restriction", "human")
# What an irreversible or restriction ask is about, set by the coordinator before it is accepted:
# "neither" is a judgment call and is refused (decide it yourself).
ASK_CLASSES = ("irreversible", "restriction_change", "neither")
ASK_REFUSED_KIND = "ask_refused"   # events: an ask the gate refused, counted in the unblocking metrics

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
            "command": {"type": "string"}, "timeout_s": {"type": "number"},
            "rewake_after_h": {"type": ["number", "null"]}, "debounce_h": {"type": ["number", "null"]},
            "issue_lifecycle": {"type": ["string", "null"]},
            "key": {"type": "string"}, "value": {"type": "string"},
            "blocking": {"type": "string", "enum": list(BLOCKING_REASONS)}, "recommendation": {"type": "string"},
            "least_disruptive": {"type": "string"}, "reversible": {"type": "boolean"}, "force": {"type": "boolean"},
            "classify": {"type": "string", "enum": list(ASK_CLASSES)},
            "resources": {"type": "array", "items": {"type": "string"}}, "exclusive": {"type": "boolean"},
            "needs_device": {"type": "boolean"}, "user_deep": {"type": "boolean"}, "standing": {"type": "boolean"},
            "continues": {"type": "integer"}, "resource": {"type": "string"}, "paused": {"type": "boolean"},
            "reason": {"type": "string"}, "supersedes": {"type": "array", "items": {"type": "string"}},
            "replaces": {"type": "string"}, "over": {"type": "string"}, "both_hold": {"type": "boolean"},
            "expires": {"type": "string"}, "until": {"type": "string"}, "until_probe": {"type": "string"},
            "source": {"type": "string"}, "match": {"type": "string"}, "hours": {"type": "number"},
            "escalate_after_h": {"type": "number"}, "below": {"type": "string"}, "why": {"type": "string"},
            "quote": {"type": "string"},
            "start_after": {"type": "string", "pattern": START_AFTER_RE}, "start_when": {"type": "string"},
            "waits_on": {"type": "string"}, "heal": {"type": ["object", "null"]}},
            "required": ["type"]}},
        "summary": {"type": "string"},
    },
    "required": ["actions"],
}

# Settings the coordinator may change on the user's explicit request. Anything else needs the
# user to edit project.json (or the web app) themselves.
def _backup_remote(v: Any) -> str:
    """delivery.backup_remote as config_set takes it: a git remote's name, or "" (off)."""
    v = "" if v is None or v is False else str(v).strip()
    if why := push.backup_problem({"backup_remote": v}):
        raise ValueError(why)
    return v


USER_SETTABLE = {
    "budget.daily_usd": float, "budget.weekly_usd": float, "budget.reserve_pct": float,
    "budget.global_daily_usd": float, "budget.day_start": str, "budget.timezone": str,
    "budget.max_parallel_workers": int, "notify.slack": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    "notify.slack_min_severity": str, "notify.chat_min_severity": str, "core_provider": str,
    "coordinator.tier": str, "coordinator.model": str, "coordinator.effort": str,
    "coordinator.unblock_effort": str, "jev.enabled": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    # Jev's 'routine or needs thought?' check on coordinator turns (coordcheck): on, off or auto.
    "jev.uses.coord_effort": lambda v: jevuse.mode({"jev": {"uses": {"x": v}}}, "x"),
    # Where code tasks branch from: the project's working branch once it has one.
    "delivery.base_ref": str,
    # Where `ttp push` publishes (required; never main, master or the remote's default branch) and
    # the commands that must pass first.
    "delivery.push_branch": str,
    "delivery.push_checks": lambda v: push.checks_of(v),
    # The files holding the version that `ttp push` bumps once above the tip, plus a changeset.
    "delivery.version_bump": lambda v: push.bump_of(v),
    # The daemon-owned push queue: reviews approve, the daemon pushes in batches, then runs after_push.
    # Not in NEEDS_USER: the queue changes who pushes, not whether a reviewed change may land, and
    # after_push has the same trust as push_checks.
    "delivery.push_queue": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    "delivery.push_batch_s": lambda v: push_queue_number("push_batch_s", v),
    "delivery.push_batch_max": lambda v: push_queue_number("push_batch_max", v),
    "delivery.push_min_gap_s": lambda v: push_queue_number("push_min_gap_s", v),
    "delivery.after_push": lambda v: push.checks_of(v),
    "delivery.after_push_timeout_s": lambda v: push_queue_number("after_push_timeout_s", v),
    # A git remote each finished code task's branch is backed up to, fast-forward only; "" turns it off.
    "delivery.backup_remote": lambda v: _backup_remote(v),
    # Branches each push also fast-forwards to the pushed commit (e.g. a 'last best' main); [] turns it off.
    "delivery.fast_forward_also": lambda v: v,   # checked against delivery.push_branch in config_set
    # Lets ttp push and the push queue publish to a push branch that is main, master or the remote's default.
    "delivery.allow_protected_push_branch": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    # Code tasks whose spec asks for it land on delivery.push_branch with `ttp push` themselves.
    "delivery.code_tasks_may_push": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    # The runaway valve on task creation; the coordinator may raise it within MAX_TASKS_PER_DAY.
    # 0 stops new tasks.
    "coordinator.max_new_tasks_per_day": lambda v: max(0, min(int(v), MAX_TASKS_PER_DAY)),
    # The separate valve on review tasks; unset means twice max_new_tasks_per_day.
    "coordinator.max_review_tasks_per_day": lambda v: max(0, min(int(v), 2 * MAX_TASKS_PER_DAY)),
    # Whether the daemon queues each finished code task's review, and what every such review also does.
    "review.auto": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    "review.auto_notes": str,
    # Skill plugins loaded for this project's workers only (a plan may recommend them).
    "providers.claude.plugin_dirs": lambda v: existing_dirs(dir_list(v)),
    # Workers load none of the user's own MCP servers, plugins, hooks or settings.
    "providers.claude.worker_isolation": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    "providers.codex.worker_isolation": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    # MCP servers from the user's own Claude config that isolated workers still get, by name.
    "providers.claude.mcp_servers": lambda v: name_list(v, strict=True),
    # Tools workers and reviewers may not call, on top of the built-in denylist (mcp__<server>__*).
    "providers.claude.disallowed_tools": lambda v: tool_list(v, strict=True),
    # Hours before an unanswered ask registered with a default falls back to it; 0 turns it off.
    # New asks never get a default, so this only drains asks registered with one.
    "coordinator.ask_timeout_h": float,
    # Days a user's yes to a charter change that failed to apply stays valid for a retry (_charter_approval).
    "coordinator.charter_approval_days": float,
}

CHARTER_HISTORY = ends.CHARTER_HISTORY   # harness file: charter sections replaced or retired
# kv: [{"id", "messages", "ask", "section", "quote", "replaces", "candidates", "text", "sha", "ts",
#       "failed", "used"}]: charter changes made in the user's turn that failed to apply. A later
# turn without a user message may apply the same change once (_charter_approval). Written only
# by apply() in a turn the daemon started for user messages; not a PR approval (prguard).
CHARTER_APPROVALS_KEY = "charter_approvals"
CHARTER_APPROVAL_DAYS = 7.0
REJECTED_KEY = "rejected_actions"   # kv: the last turn's rejected actions, shown in the next digest
# kv: {"at": ts, "why": text}: a rejected action whose blocking condition clears at a known time.
# The daemon wakes the coordinator then, so the turn's undone work does not wait for an idle wake.
RETRY_WAKE_KEY = "rejected_retry_wake"
# What makes a coordinator turn tricky or blocking: such a turn runs at least at
# coordinator.unblock_effort (effort_triggers). Events of these kinds, by the trigger they count as.
# Everything else (a task done, notes, follow-ups, a retry wake) is routine and keeps the base effort.
EFFORT_EVENT_TRIGGERS = {
    # stuck work
    "task_blocked": "stuck", "task_failed": "stuck", "task_changes_needed": "stuck", "task_review": "stuck",
    "dead_dependency": "stuck",
    "deferral_expired": "stuck", "deferral_probe_broken": "stuck", "review_stall": "stuck",
    "wait_stale": "stuck", "hold_probe_broken": "stuck", "probe_never_passes": "stuck",
    "start_resource_stale": "resource",
    "resource_trouble": "resource", "machine_condition": "resource", "machine_change_overdue": "resource",
    # costly or irreversible decisions
    "task_budget_exhausted": "costly", "ask_timeout": "costly", "pr_findings": "costly", "pr_clean": "costly",
    "pr_unapproved_ready": "costly", "after_push_failed": "costly", "push_batch_died": "costly",
    "push_tip_failed": "costly",
    # conflicting instructions: an appended rule a standing Restrictions item would override
    "charter_conflict": "conflict", "charter_retry": "conflict",
}
UNBLOCK_KINDS = frozenset(EFFORT_EVENT_TRIGGERS)
EFFORT_SEVERITIES = ("high", "critical")   # an event or alert this severe is never routine
RESOURCE_WAITS_ONLY = "waits only"   # a resource_trouble line that only counts waits is not trouble
# A user message naming a change of plan may contradict the charter or memory.
CONFLICT_RE = re.compile(r"\b(change of plan|change(d)? (my|the) mind|supersed\w*|scrap (that|this|it)|"
                         r"ignore (what|my|the) (i said|earlier|previous)|instead of|no longer|"
                         r"forget (that|what i said)|overrid\w*|contrary to|reverse (that|the) decision)\b", re.I)
EFFORT_SEEN_KEY = "effort_seen"   # kv: state-based triggers already raised once (held queue, red gates, waits)
# A task's stint of external waits ends at any other hand-off (wait_raises); looked back this far.
STINT_KINDS = ("task_waiting", "task_blocked", "task_review", "task_done", "task_failed", "task_changes_needed",
               "task_queued", "task_requeued", "push_queued")
STINT_LOOKBACK_S = 14 * 86400
ESCALATE_KEY = "escalate"   # kv: a routine turn's escalation; the next turn reruns its batch at high effort
ESCALATIONS_KEY = "escalations"   # kv: {"n": routine turns escalated, "refused": escalations refused}
EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")
NOTES_KEY = "action_notes"   # kv: the last turn's notes on actions applied with a change; information only
RECENT_OUT = 5                       # outbound messages the digest repeats, so turns do not resend them
# Digest row lengths. Background rows are cut; new events, asks and open-task notes carry decisions.
NOTE_CHARS = 140
FINISHED_ROWS, FINISHED_CHARS = 10, 120
FINISHED_DETAIL = 3                  # newest finished tasks shown with a summary; older ones by title
TITLE_CHARS = 120
SENT_CHARS = 100
MUTE_CHARS = 200
# Every coordinator turn is a fresh session whose digest the provider writes to its cache whole.
# A routine turn sees these background sections as one line while they are as the previous turn
# saw them (`digest_seen`). The budget gate, open asks and tasks, new events, paused resources and
# memory changes are always shown whole; so is everything on a turn at raised effort.
DIGEST_SEEN_KEY = "digest_seen"   # kv: {section: hash of what the previous turn's digest showed}
COLLAPSIBLE = ("recurring", "muted", "memory_budget", "machine_ledger")
EVENT_CHARS = 1500
# A plan's product arrives as these events; the daemon sizes them to fit, so they show whole.
EVENT_CHARS_BY_KIND = {"followup_proposed": 4300, "task_notes": 6000, "upstream_note": 4300}
HANDOFF_KINDS = ("task_done", "task_failed", "task_changes_needed", "task_cancelled", "cancelled_but_done")
# Events that may wait up to coordinator.batch_s for company while no worker slot would sit idle.
# Failed, blocked and review hand-offs, high or critical events and user messages wake at once.
BATCH_KINDS = ("task_done", "followup_proposed", "task_notes", "observation")


def batchable(kind: str, severity: str) -> bool:
    return kind in BATCH_KINDS and severity in ("low", "normal")
MAX_TASKS_PER_DAY = 1000
ASK_DEFAULTS_KEY = "ask_defaults"   # kv: {ask message id: recommendation}; no new ask is added
_DEFAULT_NOTE = "\n\nIf there is no answer within "
# Shown so the user can answer in one word; never applied without their answer.
_REC_NOTE = "\n\nMy recommendation: "
_LEAST_NOTE = "\n\nLeast-disruptive way considered: "
_REVERSIBLE_NOTE = "\n\nReversible alternative considered: "
LEAST_DISRUPTIVE_MIN = 40   # chars: a restriction ask names the way around it and the rule it breaks
OVER_MIN = 20   # chars: retiring a restriction outside a user turn names the end that passed
# An ask recommending yes to a step it calls reversible or safe to undo: that step is the coordinator's.
_YES_RE = re.compile(r"^\W*(yes|y|ok|okay|go|approve|proceed)\b", re.I)
_UNDOABLE_RE = re.compile(r"\breversible\b|\bcan (easily )?be (undone|reverted|rolled back)\b|"
                          r"\beasy to (undo|revert|roll back)\b|\bsafe(ly)? to (undo|revert|roll back)\b", re.I)
# Words that, a few words before an _UNDOABLE_RE match, turn it into "not (fully) reversible".
_UNDO_HEDGES = {"not", "no", "never", "hardly", "barely", "scarcely", "only", "partly", "partially", "nor",
                "without", "cannot", "neither", "nothing", "none"}
_NOT_UNDOABLE_RE = re.compile(r"\birreversib|\bpermanent|\bone-way\b|\bno (way back|undo|rollback|going back)\b|"
                              r"\b(cannot|can't|can not|could not|couldn't)\b( \w+){0,2} (be )?(undo|undone|revert|"
                              r"reverted|roll(ed)? back)\b", re.I)
_RETIRE_RE = re.compile(r"\bstale restriction|\bretir(e|es|ing)\b|\bno longer appl(y|ies)\b", re.I)


MEMORY_SNAPSHOT_KEY = "memory_snapshot"   # kv: the memory the coordinator's system prompt carries
MEMORY_DIGEST_HEADER = "## Memory added since the snapshot"


def _prompt_head(p: Project) -> tuple[str, str, str]:
    """The system prompt's parts other than memory: restrictions, role, charter."""
    role = (p.harness / "prompts" / "coordinator.md").read_text()
    charter = p.charter_path.read_text() if p.charter_path.exists() else "(no charter yet)"
    from .prompts import charter_without_restrictions, restrictions_block
    rules = restrictions_block(p)
    if rules:
        rules += ("\nWorkers are shown this block verbatim; when a spec touches anything it covers, "
                  "restate the relevant restriction in the spec itself.\n\n")
        charter = charter_without_restrictions(charter)
    return rules, role, charter


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def memory_view(p: Project, now: float | None = None) -> dict:
    """The memory snapshot the system prompt carries (`text`), plus the entries `added` (or
    changed) and the names `retired` since it was taken, which the digest lists instead.

    Any change to the system prompt misses the provider's prompt cache, and a miss costs about
    three hits. So a memory change waits in the digest until the snapshot is rebuilt: when no
    turn ran for `memory_refresh_s` (the cache is cold anyway), when the restrictions, role or
    charter changed (the prompt changes anyway), or when the waiting changes outgrow
    `memory_delta_chars`. The snapshot is taken by the same rules as before (pinned kinds first,
    COORDINATOR_MEMORY_CHARS)."""
    now = time.time() if now is None else now
    c = p.config()["coordinator"]
    entries = p._memory_entries()
    live = {e["name"]: _sha(e["line"]) for e in entries}
    basis = _sha("\0".join(_prompt_head(p)))
    snap = p.db.kv(MEMORY_SNAPSHOT_KEY) or {}
    taken = snap.get("entries") or {}
    added = [e for e in entries if taken.get(e["name"]) != live[e["name"]]]
    retired = [n for n in snap.get("shown") or [] if n not in live]
    waiting = sum(len(e["line"]) + 1 for e in added) + sum(len(n) + 20 for n in retired)
    last = max(float(p.db.kv("last_coordinator_turn", 0) or 0), float(snap.get("at") or 0))
    if (not snap or snap.get("basis") != basis or now - last > float(c.get("memory_refresh_s", 3300))
            or waiting > int(c.get("memory_delta_chars", 3000))):
        chosen = p.memory_select(COORDINATOR_MEMORY_CHARS)[0]
        snap = {"at": now, "basis": basis, "entries": live, "shown": [e["name"] for e in chosen],
                "text": "\n".join(e["line"] for e in chosen)}
        p.db.set_kv(MEMORY_SNAPSHOT_KEY, snap)
        added, retired = [], []
    return {"text": snap["text"], "added": added, "retired": retired}


def memory_digest_lines(view: dict) -> list[str]:
    """The digest's list of memory changes the system prompt's snapshot does not carry yet."""
    if not (view["added"] or view["retired"]):
        return []
    return ([f"{MEMORY_DIGEST_HEADER} (as binding as MEMORY; it moves there when the snapshot is "
             f"refreshed)"] + [e["line"] for e in view["added"]]
            + [f"[{n}] retired, ignore" for n in view["retired"]])


def standing_lines(p: Project) -> list[str]:
    """What the coordinator needs every turn that changes only with the charter or the settings:
    the charter's headings, the delivery rules and the machines list. It sits in the cached prompt,
    so turns read it from the cache instead of writing it again with each digest."""
    cfg, lines = p.config(), []
    heads = charter_headings(p)
    if heads:
        lines.append(f"## Charter sections (exact headings and numbers, for charter_update `replaces`): {heads}")
    if code_tasks_may_push(cfg):
        lines.append(f"## Delivery: code tasks may land on {cfg['delivery']['push_branch']} with `ttp push` "
                     f"(delivery.code_tasks_may_push): put the landing in the code task's spec, no separate task")
    if push_queue_on(cfg):
        lines.append(f"## Delivery: push queue on for {cfg['delivery']['push_branch']} (delivery.push_queue): "
                     f"review specs say \"approve for the push queue\", with no push or deploy steps")
    return lines + machines.list_lines()


def prompt_parts(p: Project, now: float | None = None) -> tuple[str, str]:
    """The stable prompt as (head, context), most stable first so a change misses the cache only
    from where it is: the restrictions and role change on upgrades and restriction edits; the
    charter, the standing settings and the memory snapshot (`context`) more often. A provider that
    can mark a cache breakpoint after `context` sends it as its own block after the system prompt
    (`head`)."""
    rules, role, charter = _prompt_head(p)
    standing = "\n".join(standing_lines(p))
    standing = f"# STANDING\n{standing}\n\n" if standing else ""
    memory = memory_view(p, now)["text"] or "(no memories yet)"
    return f"{rules}{role}", f"# CHARTER\n{charter}\n\n{standing}# MEMORY\n{memory}\n"


def system_prompt(p: Project, now: float | None = None) -> str:
    """Stable across turns so the provider can cache it: memory comes from the snapshot."""
    return join_prompt(*prompt_parts(p, now))


def join_prompt(head: str, context: str) -> str:
    """The whole system prompt, for a provider that cannot cache `context` as a block of its own."""
    return f"{head}\n\n{context}"


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


def digest(p: Project, gates: dict, event_ids: list[int], msg_ids: list[int], seen: dict | None = None) -> str:
    return "\n".join(text for _, text in digest_parts(p, gates, event_ids, msg_ids, seen)[0])


def digest_parts(p: Project, gates: dict, event_ids: list[int], msg_ids: list[int],
                 seen: dict | None = None) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """The turn's digest as (section, text) pairs in order, and the hash of each COLLAPSIBLE section
    shown in full, to store as `seen` for the next turn. With `seen` (a routine turn), a collapsible
    section whose hash is unchanged is shown as one line."""
    db = p.db
    now = time.time()
    shown: list[tuple[str, list[str]]] = []
    shas: dict[str, str] = {}

    def section(key: str, body: list[str], ident: str | None = None, short: str | None = None) -> None:
        """`ident`: what makes a collapsible section different for the coordinator (no counters or
        clocks); `short`: its one line when it is as the previous turn saw it."""
        if not body:
            return
        if key in COLLAPSIBLE and ident is not None:
            shas[key] = _sha(ident)
            if seen is not None and short and seen.get(key) == shas[key]:
                body = [short]
        shown.append((key, body))

    lines = [f"# STATE at {time.strftime('%Y-%m-%d %H:%M %Z')}",
             "## Project budget (authoritative; your own turn's small spend limit is NOT this budget)"]
    b = p.config()["budget"]
    for prov, g in gates.items():
        n = g.get("numbers", {})
        if g.get("regime") == "windows":
            parts = []
            for w in n.get("plan") or []:
                left = f"{w['hours_left']:.1f} h" if w.get("hours_left") is not None else "unknown time"
                burn = (f"~{w['per_worker_per_h']}/h per worker" if w.get("per_worker_per_h") is not None
                        else "no burn measured yet")
                parts.append(f"{w['window']} {w['utilization']}% used, {w.get('headroom')} points to the line, "
                             f"resets in {left}, {burn}")
            starts = "new starts allowed" if g.get("allow_new_work") and n.get("starts") else "no new starts"
            money = ("plan windows (unused capacity is lost at each reset; all slots run up to the "
                     f"{n.get('limit')}% line, which is never crossed): " + "; ".join(parts) + f" · {starts}")
            if g.get("level") == "green":
                money += (f" · {n.get('running', 0)} of {g['max_parallel']} worker slots busy: keep enough "
                          f"independent tasks ready to fill the free ones")
        else:
            est = f" (${n['estimated_24h']:.2f} of it estimated)" if n.get("estimated_24h") else ""
            day = (f"${n['spent_today']:.2f} of ${b.get('daily_usd')} today" if "spent_today" in n
                   else f"${n.get('spent_24h', 0):.2f} of ${b.get('daily_usd')} per 24h")
            money = (f"project caps (usage-billed providers together): {day}"
                     f"{est}, ${n.get('spent_7d', 0):.2f} of ${b.get('weekly_usd')} per 7 days")
            if "global_today" in n:
                stale = f", {len(n['global_stale'])} machine(s) stale" if n.get("global_stale") else ""
                money += (f" · global daily cap (whole account): ${n['global_today']:.2f} of "
                          f"${float(n.get('global_cap') or 0):.0f} today ({n.get('global_includes') or 'this account'}"
                          f"{stale})")
        lines.append(f"- {prov}: {g['level']} ({'; '.join(g['reasons']) or 'ok'}) · {money} · max_tier={g['max_tier']} "
                     f"max_parallel={g['max_parallel']} optional_work={'yes' if g['allow_optional'] else 'no'}")
    lines.append(f"- per-task default budgets: {b.get('task_default_usd')}")
    section("budget", lines)
    lines = []
    boots = db.boots(now - 86400)
    if boots:
        at_each = "; ".join(f"{time.strftime('%H:%M', time.localtime(x['ts']))} "
                            f"{', '.join(x.get('held') or []) or 'nothing'}" for x in boots)
        lines.append(f"## Host: {host_line(boots)[len('host: '):]}; held at each: {clip(at_each, 600)}")
    disk = db.kv("disk")
    if disk:
        low = db.kv("disk_low")
        guard = (f"LOW since {(now - float(low.get('since') or now)) / 3600:.1f}h: only question and plan tasks start "
                 f"until {disk.get('resume_gb')} GB are free; free space before queueing code work"
                 + (f". {clip(low['usage'], 600)}" if low.get("usage") else "")
                 if low else f"ok (guard below {disk.get('threshold_gb')} GB)")
        kept = db.kv("worktrees_kept") or {}
        # Why each is kept matters only when space runs low.
        held = ("; finished tasks' worktrees kept: " + clip(", ".join(f"#{t} ({why})" for t, why in kept.items()), 400)
                if kept and low else f"; {len(kept)} finished tasks' worktrees kept (hand-off artifacts or open "
                f"tasks): " + clip(", ".join(f"#{t}" for t in kept), 200) if kept else "")
        lines.append(f"## Disk: {disk.get('free_gb')} GB free of {disk.get('total_gb')} GB; {guard}{held}")
    section("host", lines)
    lines = []
    paused = db.paused_resources()
    if paused:
        lines.append("## Paused resources (tasks using one are not dispatched; `ttp lock` refuses it)")
        for name, v in sorted(paused.items()):
            lines.append(f"- {name}: paused {(now - float(v.get('since') or now)) / 3600:.1f}h ago by "
                         f"{v.get('by') or 'user'}" + (f" in project {v.get('project')} (shared by all projects)"
                                                      if v.get("shared") else "") + (f": {clip(v['reason'], NOTE_CHARS)}" if v.get("reason") else "")
                         + (f" ({pauseends.describe(v)})" if pauseends.describe(v) else "")
                         + (" (its end passed; the user's pause: only they lift it, no ask)" if v.get("by") == "user"
                            and v.get("until") and float(v["until"]) <= now else ""))
    section("paused", lines)
    section("resources", machines.digest_lines(db, paused, now))
    lines = []
    for res, got in shared.mismatches(p).items():
        lines.append(f"## Shared resource {res}: projects give different slot counts ("
                     + ", ".join(f"{k} {n}" for k, n in sorted(got.items())) + f"); all use the smallest, "
                     f"{min(got.values())}")
    section("shared", lines)
    lines = machine_ledger.digest_lines(p, now)
    if lines:
        kept = [ln for ln in lines if not ln.startswith("No recovery owner")]   # named once: not a change
        section("machine_ledger", ["## Shared machines in your Resources: open changes (`ttp machines change`), "
                                   "conditions, owners"] + [f"- {ln}" for ln in lines], "\n".join(kept),
                f"## Shared machines in your Resources: {len(kept)} open (as last turn)")
    section("memory_added", memory_digest_lines(memory_view(p, now)))
    section("charter_conflicts", charter_conflict_lines(charter_conflicts(p)))
    section("ends", ends.digest_lines(p, float(db.kv("last_coordinator_turn", 0) or 0), now))
    section("heal", heal.digest_lines(db, float(db.kv("last_coordinator_turn", 0) or 0), now))
    mem = memory_budget_line(p)
    if mem:
        section("memory_budget", [mem], mem, mem.split(";")[0] + " (as last turn)")
    lines = ["## Open tasks (id | status | tier | priority | age | title | last note)"]
    rows = db.q("SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled') ORDER BY priority, id LIMIT 60")
    in_review = db.review_since()
    for t in rows:
        note = clip(t["blocked_reason"] or load_result(t["result"]).get("summary"), NOTE_CHARS)
        held = anchors.anchor(t) if t["status"] == "blocked" else None
        note = f"{anchors.describe(*held)}: {note}" if held else note
        cont = continues_id(t)
        title = clip(t["title"], TITLE_CHARS) + (f" (continues #{cont})" if cont else "")
        starts = (starts_text(t, now) if t["status"] == "queued" else
                  f"{(now - in_review[t['id']]) / 3600:.1f}h in review" if t["id"] in in_review else "")
        lines.append(f"- #{t['id']} | {t['status']}{f' ({starts})' if starts else ''} | {t['tier']} | p{t['priority']} | "
                     f"{(now - t['created']) / 3600:.1f}h | {title} | {note}")
    if not rows:
        lines.append("- (none)")
    section("tasks", lines)
    events = (db.q(f"SELECT * FROM events WHERE id IN ({','.join('?' * len(event_ids))}) ORDER BY id", event_ids)
              if event_ids else [])
    # A task finishing this turn has its full hand-off under NEW EVENTS. Only a hand-off counts:
    # a batch of at most max_events_per_turn can split it from its follow-ups or a retry event.
    in_events = {e["source"] for e in events if e["kind"] in HANDOFF_KINDS}
    finished = db.q("SELECT * FROM tasks WHERE status IN ('done','failed','cancelled') AND updated>? "
                    "ORDER BY updated DESC LIMIT ?", (now - 172800, FINISHED_ROWS))
    lines = ["## Recently finished (last 48h, newest first; older ones by title)"] if finished else []
    for k, t in enumerate(finished):
        summary = ("see new events" if f"task:{t['id']}" in in_events
                   else clip(load_result(t["result"]).get("summary"), FINISHED_CHARS) if k < FINISHED_DETAIL else "")
        lines.append(f"- #{t['id']} {task_outcome(t)}: {clip(t['title'], TITLE_CHARS)}" + (f" — {summary}" if summary else ""))
    section("finished", lines)
    recurring = sched.with_costs(db)
    lines = ["## Recurring"] if recurring else []
    for s in recurring:
        lines.append(f"- {s['name']} ({s['kind']}, every {s['every_s'] // 60} min, "
                     f"{'on' if s['enabled'] else 'off'}, 7d cost ${s['cost_7d']}): {clip(s['description'], 100)}")
    section("recurring", lines, json.dumps([[s["name"], s["kind"], s["every_s"], bool(s["enabled"]),
                                             s["description"]] for s in recurring]),
            "## Recurring (as last turn): " + ", ".join(f"{s['name']} {'on' if s['enabled'] else 'off'}"
                                                        for s in recurring))
    muted = scr.mutes(db, now)
    lines = ["## Muted observations (recorded and counted, never wake you; one high event if a condition "
             "persists past its ask time; one summary event when each ends)"] if muted else []
    for m in muted:
        lines.append(f"- {clip(scr.mute_line(m, now), MUTE_CHARS)}")
    section("muted", lines, json.dumps([[m["source"], m["match"], m["below"], m["until"], m.get("why")] for m in muted]),
            "## Muted observations (as last turn; never wake you): "
            + "; ".join(f"{m['source']} {m['match']!r}: {int(m['count'])} muted, "
                        + (f"persisting {', '.join(scr.mute_ages(m, now))}, " if scr.mute_ages(m, now) else "")
                        + f"ends in {max(0.0, (float(m['until']) - now) / 3600):.1f} h" for m in muted))
    # Open asks of any age: one still waits on the user however long ago it was sent.
    lines = []
    blockers = db.q("SELECT * FROM messages WHERE kind='ask' AND handled=0 ORDER BY id DESC LIMIT 10")
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
    section("asks", lines)
    lines = []
    sent = db.q("SELECT * FROM messages WHERE direction='out' AND kind IN ('reply','ask','alert') "
                "ORDER BY id DESC LIMIT ?", (RECENT_OUT,))
    if sent:
        lines.append("## Recently sent to the user (do not repeat these)")
        for m in reversed(sent):
            lines.append(f"- {m['kind']} #{m['id']}, {(now - m['ts']) / 3600:.1f}h ago: {clip(m['text'], SENT_CHARS)}")
    section("sent", lines)
    lines = []
    chats = db.q("SELECT id, label, last_active FROM chats ORDER BY last_active DESC LIMIT 10")
    if chats:
        lines.append("## Chats attached")
    for c in chats:
        lines.append(f"- {c['id']} ({c['label'] or 'chat'}), active {(now - (c['last_active'] or now)) / 60:.0f} min ago")
    section("chats", lines)
    lines = []
    if any(e["kind"] == "upstream_note" or e["kind"] in ("followup_proposed", "task_notes")
           and "upstream:" in e["text"].lower() for e in events):
        lines.append(upstream.digest_line(p, p.config()))
    section("upstream", lines)
    lines = ["\n# NEW EVENTS"]
    if msg_ids:
        for m in db.q(f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(msg_ids))}) ORDER BY id", msg_ids):
            prov = m["provenance"] or "unknown"
            ok = "yes" if prov in prguard.APPROVING else "no, ask again on an approving channel"
            lines.append(f"- [user message #{m['id']} via {m['channel']}, provenance={prov}, can approve a PR: {ok}, "
                         f"chat={m['chat'] or '-'}] {m['text']}")
    for e in events:
        cap = EVENT_CHARS_BY_KIND.get(e["kind"], EVENT_CHARS)
        text = e["text"] if len(e["text"]) <= cap else e["text"][:cap] + " … [cut]"
        lines.append(f"- [{e['kind']} from {e['source']}, severity {e['severity']}] {text}")
    rejected = db.kv(REJECTED_KEY, []) or []
    for x in rejected:
        lines.append(f"- [your previous turn's action was rejected; fix or drop it] {x[:500]}")
    for x in db.kv(NOTES_KEY, []) or []:
        lines.append(f"- [note on your previous turn's action: applied, nothing to fix] {x[:500]}")
    if not msg_ids and not event_ids and not rejected:
        lines.append("- (none: periodic check — keep work flowing if the charter has unfinished goals)")
    lines.append("\nRespond with the JSON actions object only.")
    section("events", lines)
    return [(key, "\n".join(body)) for key, body in shown], shas


def digest_size(parts: list[tuple[str, str]]) -> dict:
    """A digest's size for the run's note: characters per section, in total, and estimated tokens
    (about four characters each), which a turn writes to the provider's cache."""
    sizes = {key: len(text) + 1 for key, text in parts}
    chars = sum(sizes.values()) - 1 if sizes else 0
    return {"chars": chars, "tokens": (chars + 3) // 4, "sections": sizes}


def _norm_severity(s: str | None) -> str:
    return s if s in SEVERITY_RANK else "normal"


# Settings the coordinator may change only in a turn that carries the user's message: a turn woken by
# logs, pull requests or a worker's hand-off can be steered by text from outside. Each maps to the
# ask_user blocking reason that fits it: the budget keys spend the user's money or eat into their
# reserve; code_tasks_may_push lets work land without a separate review. Turning that flag off is the
# safe direction and needs no one's word. Everything else the coordinator decides on its own.
NEEDS_USER = {"budget.daily_usd": "spend", "budget.weekly_usd": "spend", "budget.reserve_pct": "spend",
              "budget.global_daily_usd": "spend",
              "delivery.code_tasks_may_push": "review", "delivery.backup_remote": "access",
              "delivery.fast_forward_also": "restriction", "delivery.allow_protected_push_branch": "restriction"}
SAFE_WHEN_OFF = {"delivery.code_tasks_may_push", "delivery.backup_remote", "delivery.fast_forward_also",
                 "delivery.allow_protected_push_branch"}


def tasks_made(db, since: float, review: bool = False) -> list[float]:
    """Creation times, oldest first, of the tasks that count toward max_new_tasks_per_day, or with
    `review` toward max_review_tasks_per_day. Reviews have their own, higher cap: they check work
    already done and are what lets it be delivered, but a runaway turn must still stop. Reviews the
    daemon queued for finished code tasks count too."""
    return [r["created"] for r in db.q("SELECT created FROM tasks WHERE origin IN ('coordinator','daemon') AND "
                                       + ("kind='review'" if review else "kind!='review'")
                                       + " AND created>? ORDER BY created", (since,))]


def task_cap(cfg: dict, review: bool = False) -> int:
    """The rolling 24 h cap on new tasks of the coordinator, or on its review tasks."""
    c = cfg["coordinator"]
    cap = int(c.get("max_new_tasks_per_day", 40))
    if review:
        cap = int(c["max_review_tasks_per_day"]) if c.get("max_review_tasks_per_day") is not None else 2 * cap
    return max(0, cap)


# How far off next_task_slot puts the next slot when the cap is 0: no new tasks until it is raised.
NO_SLOT_S = 10 * 365 * 86400


def next_task_slot(db, cap: int, now: float | None = None, review: bool = False) -> float | None:
    """When the rolling 24 h task cap next allows a new task, or None if it allows one now.
    A cap of 0 allows none: its next slot is far in the future."""
    now = time.time() if now is None else now
    if cap <= 0:
        return now + NO_SLOT_S
    made = tasks_made(db, now - 86400, review)
    if len(made) < cap:
        return None
    # The cap allows a task again once enough of the oldest ones leave the window.
    return made[len(made) - cap] + 86400


def parse_start_after(raw: Any, now: float | None = None) -> float | None:
    """`start_after` as epoch seconds, or None for `now` and times already past. A delay counts
    from now; an ISO time without a zone is local."""
    now = time.time() if now is None else now
    s = str(raw).strip()
    if isinstance(raw, str) and s.lower() in ("", "now"):
        return None
    if not isinstance(raw, str) or not re.fullmatch(START_AFTER_RE, s):
        raise ValueError(f"start_after {raw!r}: use a delay such as 90m, 6h or 3d, an ISO date or time "
                         f"such as 2026-10-05T09:00, or now")
    if s == "now":
        return None
    m = re.fullmatch(r"([0-9]+) ?([smhdw])", s)
    if m:
        at = now + int(m[1]) * sched._UNIT[m[2]]
    else:
        iso = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", s.replace(" ", "T", 1).replace("Z", "+00:00"))
        try:
            at = datetime.fromisoformat(iso).timestamp()
        except ValueError as e:
            raise ValueError(f"start_after {raw!r}: {e}") from None
    if at > now + MAX_DEFER_S:
        raise ValueError(f"start_after {raw!r} is more than {MAX_DEFER_S // 86400} days away")
    return at if at > now else None


def _start_args(a: dict, cur: dict) -> tuple[float | None, str | None]:
    """A task_add/task_update's deferral: (start_after, start_when), each kept from `cur` (the
    task's current deferral) when the action leaves it out. `now` and an empty probe clear them."""
    after = parse_start_after(a["start_after"]) if a.get("start_after") is not None else cur.get("after")
    when = a.get("start_when")
    if when is None:
        when = cur.get("when")
    else:
        check_probe(when)
        if when.strip().lower() == "now":   # `now` clears the probe, as for start_after; never a command
            when = ""
    return after, (when.strip() or None) if when else None


HOLD_NEEDS_ANCHOR = ("a hold needs `waits_on`: ask:<id> (an open ask, or ask:new for the ask_user of this turn), "
                     "resource:<name>, until:<time> or when:<probe>. A hold never replaces an ask or a decision: "
                     "decide it yourself (requeue or cancel, memory_add the decision), or ask_user with a valid "
                     "blocking category and waits_on ask:new, or set an end with until or when")


def _hold_anchor(db, task: dict, a: dict, turn_asks: list[tuple[int, int]], later_ask: bool) -> str | None:
    """The `waits:<kind>:<value>` label of a task_update that sets or keeps a task blocked with `waits_on`
    (anchors.py), or None when it sets none. `ask:new` names this turn's newest ask so far, or, with an
    ask_user later in the turn, stays `ask:new` until apply re-points it. Raises when the update would
    hold the task on nothing: blocked with no anchor (unless it keeps its own), or an anchor that is not
    a valid one."""
    raw = a.get("waits_on")
    raw = raw.strip() if isinstance(raw, str) else ""
    status = a.get("status") if a.get("status") in ("queued", "blocked", "cancelled", "done", "waiting") else None
    if not raw:
        if status == "blocked" and not (task["status"] == "blocked" and anchors.anchor(task)):
            raise ValueError(f"#{task['id']} rejected: {HOLD_NEEDS_ANCHOR}")
        return None
    if (status or task["status"]) != "blocked":
        raise ValueError(f"#{task['id']} rejected: `waits_on` goes with status blocked")
    kind, _, value = raw.partition(":")
    kind, value = kind.strip().lower(), value.strip()
    if kind not in anchors.KINDS or not value:
        raise ValueError(f"#{task['id']} rejected: waits_on {raw[:80]!r} is not one of ask:<id>, resource:<name>, "
                         f"until:<time> or when:<probe>")
    if kind == "ask":
        if value.lower() == anchors.NEW_ASK:
            if turn_asks:
                return anchors.label("ask", str(turn_asks[-1][1]))
            if later_ask:
                return anchors.label("ask", anchors.NEW_ASK)
            raise ValueError(f"#{task['id']} rejected: waits_on ask:new, but this turn sends no ask_user")
        mid = value.lstrip("#")
        if not mid.isdigit() or not db.one("SELECT id FROM messages WHERE id=? AND kind='ask' AND handled=0",
                                           (int(mid),)):
            raise ValueError(f"#{task['id']} rejected: waits_on {raw[:80]!r}: no open ask #{mid}")
        return anchors.label("ask", mid)
    if kind == "resource":
        if not RESOURCE_RE.fullmatch(value):
            raise ValueError(f"#{task['id']} rejected: waits_on resource {value[:80]!r} is not a resource name")
        return anchors.label("resource", value)
    if kind == "until":
        try:
            at = parse_start_after(value)
        except ValueError as e:
            raise ValueError(f"#{task['id']} rejected: waits_on until: {str(e).replace('start_after ', '', 1)}") \
                from None
        if at is None:
            raise ValueError(f"#{task['id']} rejected: waits_on until {value!r} is not in the future")
        return anchors.label("until", f"{at:.0f}")
    check_probe(value, "waits_on when")
    return anchors.label("when", value)


def check_probe(probe, what: str = "start_when") -> None:
    """A task's shell probe (start_when, or a waiting task's retry_when): one command, bounded."""
    if not isinstance(probe, str) or len(probe) > START_WHEN_CHARS:
        raise ValueError(f"{what} must be one shell command of at most {START_WHEN_CHARS} characters")
    if re.match(r"\s*landed:", probe) and not re.fullmatch(r"\s*landed:\s*#?\d+\s*", probe):
        raise ValueError(f"{what} {probe.strip()[:60]!r} is not a landed probe: write it as landed:#<task id>")


def start_why(a: dict, cur: dict, when: str | None) -> str | None:
    """The plain words for a start_when probe: the action's `why`, else the current one while the
    probe stays the same. None without a probe."""
    if not when:
        return None
    why = a.get("why") if a.get("why") is not None else cur.get("why") if when == cur.get("when") else None
    why = " ".join(str(why or "").split())[:START_WHY_CHARS]
    return why or None


def defer_labels(after: float | None, when: str | None, why: str | None = None) -> list[str]:
    """The labels a deferred task carries (see db.deferral); none when it may start now."""
    if after is None and not when:
        return []
    return ([f"start_after:{after:.0f}"] if after else []) + ([f"start_when:{when}"] if when else []) \
        + ([f"start_why:{why}"] if when and why else []) + [f"deferred_since:{time.time():.0f}"]


def starts_text(task: dict, now: float | None = None, plain: bool = False) -> str:
    """'starts <time>' / 'starts when: <probe>' for a task that waits to start; '' otherwise.
    `plain` (what the user sees): 'starts when <why>', or 'when a check passes', never the probe."""
    now = time.time() if now is None else now
    d = deferral(task)
    after = d.get("after") if (d.get("after") or 0) > now and (task.get("not_before") or 0) > now else None
    if not after and not d.get("when"):
        return ""
    if not d.get("when"):
        when = ""
    elif plain:
        landed = re.fullmatch(r"landed:#([0-9]+)", d["when"].strip())
        when = (f"when {clip(d['why'], 160)}" if d.get("why") else f"when #{landed[1]} lands" if landed
                else "when a check passes")
    else:
        when = f"when: {clip(d['when'], 160)}" + (f" ({clip(d['why'], 160)})" if d.get("why") else "")
    return "starts " + (f"{_clock(after)}" + (f", then {when}" if when else "") if after else when)


def _clock(ts: float) -> str:
    """A local time: today's as HH:MM, another day's with its date."""
    same_day = time.strftime("%Y-%m-%d", time.localtime(ts)) == time.strftime("%Y-%m-%d")
    return time.strftime("%H:%M" if same_day else "%Y-%m-%d %H:%M", time.localtime(ts))


def apply(p: Project, actions: list[dict], default_chat: str | None = None, user_turn: bool = False,
          turn: int | None = None, messages: list[int] | None = None) -> list[str]:
    """Apply validated actions. Returns human-readable notes about rejected ones, fed back next turn.
    Notes on actions applied with a change (NOTES_KEY) reach the next digest as information only.
    `messages`: the user messages the daemon started this turn for (a charter change they approved
    that fails here stays approved for a retry, see _charter_approval).

    A turn cut off before its database transaction commits is applied again from its output. Its
    file writes carry `turn`.<action index> so the replay does not repeat them, while the same
    text sent again by a later turn is still written."""
    db, problems, notes = p.db, [], []
    cfg = p.config()
    # For the stale-restriction check (_restriction_conflicts): the restrictions before this turn's
    # first charter_update, the text it added, and whether it edited a restriction in place.
    restr_before: str | None = None
    rules_added: list[tuple[str, str]] = []
    restr_edited = False
    replies: list[int] = []
    # config_set goes first so a cap raised in this turn counts for this turn's task_add actions.
    # The index stays the original one so replay keys do not change.
    order = sorted(enumerate(actions), key=lambda ia: (ia[1] or {}).get("type") != "config_set")
    turn_asks: list[tuple[int, int]] = []   # (position in order, ask id) of this turn's asks, for `waits_on ask:new`
    new_ask_holds: list[tuple[int, dict, dict]] = []   # (position, task before, update) waiting for a later ask
    for k, (i, a) in enumerate(order):
        t = a.get("type")
        key = f"{turn}.{i}" if turn is not None else None
        try:
            if t == "reply":
                chat = a.get("chat") or default_chat
                replies.append(db.post("out", a["text"], chat=None if chat in (None, "all") else chat, kind="reply",
                                       severity=_norm_severity(a.get("severity") or "normal")))
            elif t == "task_add":
                title = (a.get("title") or "").strip()
                if not title:
                    raise ValueError("task_add needs a title")
                dup = db.one("SELECT id FROM tasks WHERE title=? AND status NOT IN ('done','failed','cancelled')",
                             (title,))
                if dup and str(dup["id"]) != str(a.get("continues")):
                    raise ValueError(f"duplicate of open task #{dup['id']}")
                if not a.get("force"):
                    same = similar_task(db, title, a.get("spec") or "", skip=a.get("continues"))
                    if same:
                        raise ValueError(f"near duplicate of task #{same['id']} ({same['status']}) "
                                         f"'{same['title'][:120]}': update or continue it; if this is new work, say "
                                         f"how it differs in the title and spec, or add force: true")
                review = a.get("kind") == "review"
                cap = task_cap(cfg, review)
                free_at = next_task_slot(db, cap, review=review)
                if free_at is not None:
                    what = "review tasks" if review else "new tasks (reviews have their own cap)"
                    if cap <= 0:
                        why = (f"the cap on {what} is 0: none can be added until it is raised; you are "
                               f"woken when it is")
                    else:
                        why = (f"cap of {cap} {what} in 24 h reached; the next slot frees at "
                               f"{_clock(free_at)} local, when you are woken to add it again")
                    wake = db.kv(RETRY_WAKE_KEY) or {}
                    if not wake.get("at") or free_at < float(wake["at"]):
                        db.set_kv(RETRY_WAKE_KEY, {"at": free_at, "review": review,
                                                   "why": f"task_add {title!r}: {why}"})
                    raise ValueError(why)
                tier = a.get("tier") if a.get("tier") in ("light", "standard", "deep") else "standard"
                budget = a.get("budget_usd") or cfg["budget"]["task_default_usd"].get(tier, 8.0)
                kind_label = "exclusive" if a.get("exclusive") else "resource"
                names = _resource_names(a.get("resources") or [], t, problems)
                labels = [f"{kind_label}:{r}" for r in names]
                if a.get("needs_device") or set(names) & locks.device_locks(cfg):
                    labels.append("needs_device")
                if tier == "deep" and a.get("user_deep"):
                    labels.append(effort.USER_DEEP)   # a device task keeps deep on its first start
                deps = _new_dependencies(db, None, a.get("depends_on") or [])
                # The daemon would block a new task on a dead dependency at once.
                dead = db.dead_dependency(deps)
                if dead:
                    raise ValueError(f"task_add rejected: depends on #{dead[0]} which is {dead[1]}; "
                                     f"drop or replace depends_on")
                # A review of work the daemon already queued a review for replaces it while it has not
                # started; one already under way makes this a duplicate.
                covered = _covered(db, deps, a.get("spec") or "") if review else set()
                autos = _auto_reviews(db, covered)
                started = {n: r for n, r in autos.items() if r["status"] != "queued" or r["attempts"]}
                if covered and set(started) == covered:
                    ids = ", ".join(f"#{r['id']}" for r in started.values())
                    raise ValueError(f"duplicate of review {ids} the daemon queued, already under way; send it a "
                                     f"`spec` with task_update to add checks")
                old = _continued(db, a["continues"], deps) if a.get("continues") is not None else None
                followup = old if old and old["status"] == "done" else None
                if followup:
                    # Done work is not taken over: the new task is a follow-up that starts fresh
                    # (a code task from the delivery branch tip, not the old branch).
                    old = None
                # A task on another branch carries it (worktree on it, `ttp push --own` onto it): one a
                # `pr_branch: <branch>` line names, else a finished task's work branch the spec names.
                # Failed or cancelled work is continued then (taken over); done work is not.
                spec_text = a.get("spec") or ""
                given = None if review else SPEC_PR_BRANCH.search(spec_text)
                carried = None if review or given else _named_branch(db, spec_text)
                if carried and old and old["id"] == carried[0]["id"]:
                    carried = None   # it continues that branch's task already
                elif carried and a.get("continues") is None and carried[0]["status"] in ("failed", "cancelled"):
                    try:
                        old = _continued(db, carried[0]["id"], deps)
                    except ValueError:
                        old = None
                taken = bool(carried and old and old["id"] == carried[0]["id"])
                onto = given.group(1) if given else carried[1] if carried and not taken else ""
                if onto:
                    labels.append(f"pr_branch:{onto}")
                if old:
                    labels.append(f"continues:{old['id']}")
                after, when = _start_args(a, {})
                labels += defer_labels(after, when, start_why(a, {}, when))
                why = prguard.spec_problem(db, a.get("spec") or "")
                if why:
                    raise ValueError(f"task_add rejected: {why}")
                kind, pr_ask = _delivery_kind(a.get("kind") or "work", title + "\n" + (a.get("spec") or ""))
                with db.tx():
                    new_id = db.add_task(title, a.get("spec") or "", kind=kind, tier=tier,
                                         priority=int(a.get("priority") or 3), provider=a.get("provider") or None,
                                         budget_usd=float(budget), depends_on=deps,
                                         reply_chat=a.get("reply_chat") or None, origin="coordinator",
                                         labels=labels, not_before=after,
                                         parent=followup["id"] if followup else None)
                    for r in {r["id"]: r for n, r in autos.items() if n not in started}.values():
                        db.update_task(r["id"], status="cancelled", blocked_reason=f"replaced by review #{new_id}")
                        _take_over_dependents(db, r["id"], new_id)
                        notes.append(f"task_add: #{new_id} replaces review #{r['id']} the daemon had queued")
                    if old:
                        _take_over_dependents(db, old["id"], new_id)
                        if old["status"] == "blocked":   # superseded: never requeued into duplicate work
                            db.update_task(old["id"], status="cancelled", blocked_reason=f"continued by #{new_id}")
                if followup:
                    notes.append(f"task_add: #{followup['id']} is done; added #{new_id} as its follow-up")
                if carried:
                    how = f"continues #{old['id']}" if taken else f"labelled pr_branch:{carried[1]}"
                    notes.append(f"task_add: #{new_id} names #{carried[0]['id']}'s branch {carried[1]}: {how}, "
                                 f"so `ttp push --own` publishes onto it")
                if pr_ask:
                    notes.append(f"task_add: #{new_id} added as `code`, not `{a.get('kind') or 'work'}`: it asks for "
                                 f"PR delivery ({pr_ask!r}) and only code tasks open or update PRs")
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
                        prev = load_result(task["result"])
                        if task["status"] != "queued" and ("waiting_since" in prev or "stale_wakes" in prev):
                            # A requeue is a decision to run it, not to sleep on its probe, and its
                            # earlier unchanged wakes no longer count.
                            prev.pop("waiting_since", None)
                            prev.pop("stale_wakes", None)
                            upd["result"] = dump_result(prev)
                waits = _hold_anchor(db, task, a, turn_asks,
                                     any((x or {}).get("type") == "ask_user" for _, x in order[k + 1:]))
                if a.get("depends_on") is not None:
                    deps = _new_dependencies(db, task, a["depends_on"])
                    upd["depends_on"] = deps
                    # A task blocked on a dead dependency is released by re-pointing it.
                    if "status" not in upd and task["status"] == "blocked" and \
                            db.dead_dependency(dependency_ids(task)):
                        upd.update(status="queued", blocked_reason=None)
                if a.get("resources") is not None:
                    # Moves the task to other resources (a healthy machine instead of a failing one).
                    if task["status"] == "running":
                        raise ValueError(f"task #{task['id']} is running: its resources change only while it is "
                                         f"not; cancel it and re-add it with `continues` to move it now")
                    kind_label = "exclusive" if a.get("exclusive") else "resource"
                    keep = [lb for lb in json.loads(task["labels"] or "[]")
                            if not (isinstance(lb, str) and lb.split(":", 1)[0] in ("resource", "exclusive"))]
                    names = _resource_names(a["resources"], t, problems)
                    upd["labels"] = keep + [f"{kind_label}:{r}" for r in names]
                    if "needs_device" not in keep and set(names) & locks.device_locks(cfg):
                        upd["labels"].append("needs_device")
                    if upd.get("status", task["status"]) == "queued" and task["not_before"] \
                            and deferral(task).get("after") != task["not_before"]:
                        # What it waited on was the old resource: it may start on the new one now.
                        upd.update(not_before=None, blocked_reason=None)
                        prev = load_result(upd.get("result") or task["result"])
                        prev.pop("waiting_since", None)
                        upd["result"] = dump_result(prev)
                if a.get("start_after") is not None or a.get("start_when") is not None \
                        or (a.get("why") is not None and deferral(task).get("when")):
                    if task["status"] in ("running", *TERMINAL_TASK_STATES):
                        raise ValueError(f"task #{task['id']} is {task['status']}: only a task that has not "
                                         f"started can be deferred; add a new one with start_after/start_when")
                    cur = deferral(task)
                    after, when = _start_args(a, cur)
                    labels = upd.get("labels")
                    labels = json.loads(task["labels"] or "[]") if labels is None else labels
                    upd["labels"] = without_deferral(labels) + defer_labels(after, when, start_why(a, cur, when))
                    upd["not_before"] = after
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
                    why = prguard.spec_problem(db, spec)
                    if why:
                        raise ValueError(f"#{task['id']} rejected: {why}")
                    upd["spec"] = task["spec"] + "\n\n## Update\n" + spec
                    prev = load_result(upd.get("result") or task["result"])
                    if task["status"] != "running" and prev.pop("stale_wakes", None) is not None:
                        # A re-plan: the wait it hands back next is not compared with the old plan's.
                        upd["result"] = dump_result(prev)
                    kind, pr_ask = _delivery_kind(task["kind"], spec)
                    if pr_ask and upd.get("status", task["status"]) in ("queued", "blocked", "waiting"):   # not mid-run
                        upd["kind"] = kind
                        notes.append(f"task_update: #{task['id']} is now `code`, not `{task['kind']}`: its spec asks "
                                     f"for PR delivery ({pr_ask!r}) and only code tasks open or update PRs")
                    elif pr_ask:
                        notes.append(f"task_update: #{task['id']} asks for PR delivery but is running as "
                                     f"`{task['kind']}` and keeps that kind mid-run, so it cannot open the PR: "
                                     f"cancel it and task_add a code task with continues={task['id']}")
                if waits or upd.get("status", "blocked") != "blocked":
                    labels = upd.get("labels")
                    labels = json.loads(task["labels"] or "[]") if labels is None else labels
                    if waits or anchors.without(labels) != labels:
                        upd["labels"] = anchors.without(labels) + ([waits] if waits else [])
                db.update_task(task["id"], **upd)
                if waits == anchors.label("ask", anchors.NEW_ASK):
                    new_ask_holds.append((k, task, upd))
                if spec and task["status"] == "running":
                    for r in db.q("SELECT dir FROM runs WHERE task=? AND status='running'", (task["id"],)):
                        if r["dir"]:
                            _append_update(Path(r["dir"], "steer.md"), spec, key)
                if upd.get("status") == "cancelled":
                    # Only once the cancel is saved: a turn cut off before that must not stop the run.
                    # The daemon asks a cancelled task's runs to stop if this never happens.
                    db.after_commit(lambda tid=task["id"]: stop_runs(db, p.runs, tid))
            elif t == "ask_user":
                if a.get("reversible") is True:
                    raise ValueError("ask_user rejected: decide it yourself. A reversible choice is a judgment "
                                     "call: act on it, memory_add a decision and notify at severity low")
                if a.get("blocking") not in BLOCKING_REASONS:
                    raise ValueError(f"ask_user rejected: `blocking` must be one of {', '.join(BLOCKING_REASONS)}; "
                                     f"got {a.get('blocking')!r}. Anything else, decide it yourself")
                least = str(a.get("least_disruptive") or "").strip()
                text = a["text"].strip()
                why = _unclassified_ask(a, least)
                if why:
                    _count_refusal(db, key, why[0], text)
                    raise ValueError(f"ask_user rejected: {why[1]}")
                if a["blocking"] == "restriction" and len(least) < LEAST_DISRUPTIVE_MIN:
                    raise ValueError("ask_user rejected: first answer what is a reasonably non-disruptive way to "
                                     "proceed. If it fits the restrictions, take it (task_add) and memory_add the "
                                     "decision instead of asking. If not, put it in least_disruptive with the "
                                     "restriction it breaks.")
                why = _self_health_ask(a, text, least)
                if why:
                    _count_refusal(db, key, "self-health", text)
                    raise ValueError(f"ask_user rejected: {why}")
                if (a["blocking"] not in ("restriction", "review", "merge")    # a review or merge ask is never leave to open
                        and prguard.draft_permission_ask(text)):
                    raise ValueError("ask_user rejected: draft PRs need no permission: open it. Opening and updating a "
                                     "draft PR is always allowed, even under a code freeze; only leaving draft needs "
                                     "the user's yes")
                why = _needless_ask(a, text)
                if why:
                    raise ValueError(f"ask_user rejected: {why}")
                for o in db.q("SELECT id, text FROM messages WHERE kind='ask' AND handled=0"):
                    if _same_text(_ask_question(o["text"]), text):
                        raise ValueError(f"already asked as open ask #{o['id']}; it waits for the answer")
                if a["blocking"] in prguard.APPROVING_REASONS:
                    why = prguard.findings_problem(db, text)
                    if why:
                        raise ValueError(f"ask_user rejected: {why}")
                rec = (a.get("recommendation") or "").strip()
                if a["blocking"] == "restriction":
                    text += f"{_LEAST_NOTE}{least}"
                elif a["blocking"] == "irreversible" and least:
                    text += f"{_REVERSIBLE_NOTE}{least}"
                if rec:
                    text += f"{_REC_NOTE}{rec}"
                turn_asks.append((k, db.post("out", text, chat=None, kind="ask",
                                             severity=_norm_severity(a.get("severity") or "high"),
                                             ref=f"{prguard.BLOCKING_REF}{a['blocking']}")))
            elif t == "resolve":
                n = db.x("UPDATE messages SET handled=1 WHERE id=? AND kind='ask'", (int(a["id"]),))
                if not n:
                    raise ValueError(f"no open question #{a.get('id')}")
                unblock.record_resolve(db, int(a["id"]), messages or [])
            elif t == "pr_approve":
                from .slack import from_config
                prguard.approve(db, str(a.get("text") or a.get("value") or ""), int(a.get("id") or 0),
                                str(a.get("quote") or ""), slack=from_config(cfg), project=p.name)
            elif t == "notify":
                db.post("out", a["text"], chat=None, kind="alert", severity=_norm_severity(a.get("severity")))
            elif t == "memory_add":
                added = p.add_memory(a["text"], kind=a.get("memory_kind") or "fact", title=a.get("title"), key=key,
                                     end=ends.from_action(a), standing=a.get("standing") is True)
                if (a.get("memory_kind") or "") == "restriction":
                    _tell_running_workers(db, f"New binding restriction: {a['text'].strip()}", key)
                old = a.get("supersedes") or []
                for name in [old] if isinstance(old, str) else old:
                    try:
                        # A standing entry superseded by a standing one is shortened, not retired.
                        p.forget_memory(str(name), keep=added.stem, why=a.get("why") or (
                            f"shortened into [{added.stem}]" if a.get("standing") is True else None))
                    except ValueError as e:
                        raise ValueError(f"memory added, but `supersedes` failed: {e}") from None
                memory_budget_check(p)
            elif t == "memory_forget":
                p.forget_memory(str(a.get("name") or ""), why=a.get("why"))
                memory_budget_check(p)
            elif t == "charter_update" and a.get("both_hold") and not (a.get("text") or a.get("quote")
                                                                    or a.get("replaces")):
                notes.append(settle_conflicts(p, str(a.get("key") or "")))
            elif t == "charter_update":
                section = " ".join((a.get("section") or "Notes").lstrip("#").split()) or "Notes"
                text, quote = (a.get("text") or "").strip(), (a.get("quote") or "").strip()
                if not (text or quote or a.get("replaces")):
                    raise ValueError("charter_update: give `text`, or `quote` (with empty text) to remove an item")
                end = ends.from_action(a)
                if end and (quote or not text):
                    raise ValueError("charter_update: an end (`expires`, `until`, `until_probe`) goes with new `text`, "
                                     "not with `quote`")
                over = " ".join(str(a.get("over") or "").split())
                msg = f"charter ({section.lower()}): " + (text[:80] or f"removes {quote[:70]!r}")
                if key and _has_line(p.charter_path, f", turn {key})"):
                    pass   # this turn's update is already in its own dated section: a retry must not add it twice
                else:
                    if restr_before is None:
                        from .prompts import charter_restrictions
                        restr_before = charter_restrictions(p.charter_path.read_text())
                    replaces = str(a.get("replaces") or "")
                    ok, no_ok = (None, "") if user_turn else _charter_approval(p, section, quote, replaces, text)
                    text = ok["text"].strip() if ok else text   # the words the user said yes to
                    # a Restrictions item edited without the user's word or `over` is refused below anyway
                    refused = (quote and _DATED.sub("", section).lower().startswith("restriction")
                               and not (user_turn or ok or len(over) >= OVER_MIN))
                    if text and not (replaces or a.get("both_hold") or refused):
                        overlap = _reject_contradicting_append(p, section, text, user_turn, messages, ok, quote, end)
                        if overlap:
                            notes.append(overlap)
                    source = _users_own(db, messages or []) if user_turn else list((ok or {}).get("messages") or [])
                    try:
                        target, extra, retired = _charter_update(p, section, text, quote, replaces, key,
                                                                 user_turn or ok is not None, over, end, no_ok,
                                                                 source)
                    except ValueError as e:
                        if user_turn:
                            _record_charter_approval(p, messages or [], section, quote, replaces, text, end, str(e))
                        raise
                    if ok is not None:
                        _use_charter_approval(p, ok["id"], key)
                        notes.append(f"charter_update: applied on the user's yes in message "
                                     f"#{', #'.join(map(str, ok['messages']))}, recorded when it first failed")
                    msg += extra
                    if (quote and target.lower().startswith("restriction")
                            or extra.lower().startswith(" (replaces restriction")):
                        restr_edited = True
                    elif text and not quote and not a.get("both_hold"):
                        rules_added.append((target, text))
                    if retired:
                        db.post("out", f"Retired the charter restriction {retired}: {over}."
                                       + (f" Still in force: {clip(text, 300)}" if text else ""),
                                chat=None, kind="alert", severity="low")
                    if target.lower().startswith("restriction"):
                        if text:
                            _tell_running_workers(db, f"New binding restriction: {text}", key)
                        elif quote:
                            _tell_running_workers(db, f"Binding restriction retired: {quote}", key)
                p.commit_harness([p.charter_path, p.harness / CHARTER_HISTORY], msg)
            elif t == "schedule_set":
                sched.before_change(p)
                old = db.one("SELECT * FROM schedules WHERE name=?", (a.get("name"),))
                kind = a.get("kind") or (old["kind"] if old else "llm")
                kept = json.loads(old["payload"] or "{}") if old and old["kind"] == kind else {}
                # Fields the action leaves out keep their current values; only a new schedule gets defaults.
                enabled = bool(a["enabled"]) if "enabled" in a else (bool(old["enabled"]) if old else True)
                if kind == "command":
                    # The daemon runs payload.command; a schedule without one would report "no command" forever.
                    # Turning one off needs no command, so a broken schedule can always be switched off.
                    payload = {**kept, **{k: a[k] for k in ("command", "timeout_s") if a.get(k)}}
                    if "rewake_after_h" in a:   # null: a known issue never wakes again by time alone
                        payload["rewake_after_h"] = _hours_or_none(a, "rewake_after_h")
                    if "issue_lifecycle" in a:   # explicit_clear: receipts stay pending until acknowledged
                        life = a.get("issue_lifecycle") or None
                        if life not in (None, "default", scr.EXPLICIT_CLEAR):
                            raise ValueError(f"schedule_set {a.get('name')!r} rejected: `issue_lifecycle` must be "
                                             f"{scr.EXPLICIT_CLEAR!r}, 'default' or null; got {life!r}")
                        payload.pop("issue_lifecycle", None)
                        if life == scr.EXPLICIT_CLEAR:
                            payload["issue_lifecycle"] = life
                    if "heal" in a:   # null removes it
                        payload.pop("heal", None)
                        if a["heal"]:
                            heal.validate(a["heal"], f"schedule_set {a.get('name')!r} rejected: `heal`")
                            payload["heal"] = a["heal"]
                    if enabled and not str(payload.get("command") or "").strip() and not payload.get("heal"):
                        raise ValueError(f"schedule_set {a.get('name')!r} rejected: kind command needs `command`, "
                                         f"the shell command to run (and optionally `timeout_s`), or a `heal` block")
                elif kind == "llm":
                    payload = {**kept, "spec": a.get("spec") or kept.get("spec") or "",
                               "tier": a.get("tier") or kept.get("tier") or "standard"}
                    if "debounce_h" in a:   # null or 0: no debounce
                        payload["debounce_h"] = _hours_or_none(a, "debounce_h")
                elif kind == "watcher" and old:
                    payload = kept   # a built-in probe: only its timing and switch change
                else:
                    raise ValueError(f"schedule_set {a.get('name')!r} rejected: `kind` must be llm or command")
                every_s = sched.parse_every(a.get("every") or (old["every_s"] if old else "1d"))
                if str(a.get("every") or "").strip().isdigit():   # bare seconds: echo what was understood
                    notes.append(f"schedule_set {a['name']!r}: every {every_s} s")
                sched.upsert(db, a["name"], kind, every_s,
                             (a.get("at") or None) if "at" in a else (old["at"] if old else None), enabled,
                             a["budget_usd"] if "budget_usd" in a else (old["budget_usd_day"] if old else None),
                             (a.get("text") or "") if "text" in a else ((old["description"] or "") if old else ""),
                             payload)
                sched.write_file(p, f"schedule {a['name']}: {'changed' if old else 'added'}")
            elif t == "config_set":
                key = a.get("key", "")
                if key not in USER_SETTABLE:
                    from .project import unknown_key_hint
                    hint = unknown_key_hint(key)
                    raise ValueError(hint or f"{key} is not user-settable from chat")
                value = USER_SETTABLE[key](a.get("value"))
                if key == "delivery.backup_remote" and (why := push.backup_problem({**(cfg.get("delivery") or {}),
                                                                                    "backup_remote": value})):
                    raise ValueError(why)
                if key == "delivery.fast_forward_also":
                    value = push.fast_forward_check(value, str((cfg.get("delivery") or {}).get("push_branch") or ""))
                if key in NEEDS_USER and not user_turn and not (key in SAFE_WHEN_OFF and value in (False, "", [])):
                    raise ValueError(f"{key} needs the user's approval: ask_user (blocking {NEEDS_USER[key]}) with the "
                                     f"exact value, and set it in the turn that carries their yes")
                p.set_config(key, value)
                cfg = p.config()
            elif t == "resource_pause":
                if not isinstance(a.get("paused"), bool):
                    raise ValueError("resource_pause needs `paused`: true or false")
                name = str(a.get("resource") or "").strip()
                if not RESOURCE_RE.fullmatch(name):
                    raise ValueError(f"not a resource name: {name!r}")
                held = db.paused_resources().get(name)
                if not a["paused"] and held and held.get("by") == "user" and not user_turn:
                    # A pause the user set is lifted on their word only, never by text from outside.
                    raise ValueError(f"{name} was paused by the user; lift it only in the turn that carries "
                                     f"their go-ahead")
                pause_resource(p, name, a["paused"], reason=a.get("reason") or a.get("text") or "",
                               by="coordinator", key=key, end=pauseends.from_action(a) if a["paused"] else None)
            elif t == "observation_mute":
                scr.mute(db, a.get("source"), a.get("match"), a.get("hours"), a.get("below"),
                         a.get("why") or a.get("reason") or a.get("text") or "",
                         escalate_after_h=a.get("escalate_after_h"))
            elif t in ("noop", "escalate", None):
                pass   # an escalation is the daemon's (Daemon._finish_coordinator); here it changes nothing
            else:
                raise ValueError(f"unknown action {t!r}")
        except Exception as e:   # one bad action is reported back; it never aborts the turn
            problems.append(f"{t}: {e}")
    for k, before, upd in new_ask_holds:
        ask = next((mid for at, mid in turn_asks if at > k), None)
        now_labels = json.loads((db.task(before["id"]) or before)["labels"] or "[]")
        if ask is not None:
            db.update_task(before["id"], labels=anchors.without(now_labels) + [anchors.label("ask", str(ask))])
        else:   # its ask was rejected: the hold would wait on nothing, so the whole update is undone
            db.update_task(before["id"], **{f: before[f] for f in upd if f != "updated"})
            problems.append(f"task_update: #{before['id']} rejected: it waits_on ask:new, but no ask_user of this "
                            f"turn went through")
    if user_turn and rules_added and not restr_edited and restr_before:
        _restriction_conflicts(p, restr_before, rules_added)
    db.set_kv(NOTES_KEY, notes)
    if problems and replies:
        # The reply may say the work is under way; the user must not read that when it is not.
        note = clip("; ".join(problems), 240)
        db.x(f"UPDATE messages SET text=text||? WHERE id IN ({','.join('?' * len(replies))})",
             [f"\n\n(not done: {note})", *replies])
    return problems


def _delivery_kind(kind: str, text: str) -> tuple[str, str | None]:
    """The kind a task runs as, and the clause that changed it: a `work` or `question` task asked to
    open, update or publish a PR is a `code` task, the only kind that may (worker prompt)."""
    ask = prguard.delivery_instruction(text) if kind in ("work", "question") else None
    return ("code", ask) if ask else (kind, None)


def _ask_question(text: str) -> str:
    return text.split(_DEFAULT_NOTE)[0].split(_REC_NOTE)[0].split(_LEAST_NOTE)[0]


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


SIMILAR_DONE_DAYS = 3   # a task done this recently is still compared against a new task_add
_STOP = set("a an the of to for and or in on at by with from into vs via is be as it its this that".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+(?:[.-][a-z0-9_]+)*", (text or "").lower()) if w not in _STOP}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def looks_same(title1: str, spec1: str, title2: str, spec2: str) -> bool:
    """Same work under a near-identical title and spec. Titles that both name numbers (task ids,
    iterations, candidates) and name different ones are different work, however alike they read."""
    if _same_text(title1, title2):
        return True
    n1, n2 = (set(re.findall(r"\d[\w.-]*", t.lower())) for t in (title1, title2))
    if n1 and n2 and n1 != n2:
        return False
    tj = _jaccard(_words(title1), _words(title2))
    s1, s2 = _words(spec1), _words(spec2)
    # A spec of a word or two says nothing about the work; judge by the title alone then.
    sj = _jaccard(s1, s2) if s1 and s2 and len(s1 | s2) >= 4 else tj
    return (tj >= 0.7 and sj >= 0.45) or (tj >= 0.4 and sj >= 0.7)


def similar_task(db, title: str, spec: str, skip: Any = None) -> dict | None:
    """An open task, or one done in the last SIMILAR_DONE_DAYS, that a new task_add would repeat.
    Failed and cancelled ones may be redone; `skip` is the task the new one continues."""
    since = time.time() - SIMILAR_DONE_DAYS * 86400
    for t in db.q("SELECT id, title, spec, status FROM tasks WHERE status NOT IN ('done','failed','cancelled') "
                  "OR (status='done' AND COALESCE(updated, created, 0) >= ?) ORDER BY id DESC", (since,)):
        if str(t["id"]) != str(skip) and looks_same(title, spec, t["title"] or "", t["spec"] or ""):
            return t
    return None


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


def _continued(db, raw: Any, deps: list[int]) -> dict:
    """The task a new one continues, checked: only work that can no longer finish is taken over,
    and the new task must not wait on the dependents it takes over."""
    try:
        old = db.task(int(raw))
    except (TypeError, ValueError):
        raise ValueError("task_add continues must be a task id") from None
    if not old:
        raise ValueError(f"task_add continues: no task #{raw}")
    if old["status"] == "done":
        return old   # the caller adds it as a follow-up instead
    if old["status"] not in ("failed", "cancelled", "blocked"):
        raise ValueError(f"task_add continues rejected: #{old['id']} is {old['status']}; only a failed, "
                         f"cancelled or blocked task can be continued (a done one gets a follow-up)")
    if old["id"] in deps:
        raise ValueError(f"task_add cannot depend on #{old['id']}, the task it continues")
    if any(db.dependency_cycle(t["id"], deps) for t in _open_dependents(db, old["id"])):
        raise ValueError(f"task_add depends_on {deps} would create a cycle: it waits on a task that "
                         f"waits on #{old['id']}")
    return old


# A task's own work branch named in a spec (worktree.ensure names them ttp/t<id>-<slug>), and a
# spec line naming the branch a task delivers onto.
NAMED_BRANCH = re.compile(r"(?<![\w/.-])ttp/t(\d+)-[\w./-]*[\w-]")
SPEC_PR_BRANCH = re.compile(r"(?<![\w-])pr_branch:\s*`?([A-Za-z0-9][\w./-]*[\w-])`?")


def _named_branch(db, spec: str) -> tuple[dict, str] | None:
    """(task, branch) when `spec` names the work branch of one finished task (done, failed or
    cancelled; a live one still owns it); None when it names none, or branches of several tasks."""
    found: dict[int, tuple[dict, str]] = {}
    for m in NAMED_BRANCH.finditer(spec):
        t = db.task(int(m.group(1)))
        if t and t["status"] in ("done", "failed", "cancelled") \
                and m.group(0) == (t["branch"] or f"ttp/t{t['id']}-{worktree.slug(t['title'])}"):
            found[t["id"]] = (t, m.group(0))
    return next(iter(found.values())) if len(found) == 1 else None


def _open_dependents(db, task_id: int) -> list[dict]:
    return [t for t in db.q("SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled') "
                            "AND depends_on NOT IN ('', '[]')") if task_id in dependency_ids(t)]


def _covered(db, deps: list, spec: str) -> set[int]:
    """The code tasks a review covers: those it depends on and those whose branch its spec names."""
    ids = {d for d in deps if isinstance(d, int)}
    for r in db.q("SELECT id, branch FROM tasks WHERE kind='code' AND branch IS NOT NULL AND branch!=''"):
        if re.search(rf"(?<![\w/.-]){re.escape(r['branch'])}(?![\w/-])", spec):
            ids.add(r["id"])
    return ids


def _auto_reviews(db, covered: set[int]) -> dict[int, dict]:
    """Code task id in `covered` -> the open review the daemon queued for it (label auto_review:<id>)."""
    out: dict[int, dict] = {}
    if not covered:
        return out
    for r in db.q("SELECT * FROM tasks WHERE kind='review' AND origin='daemon' "
                  "AND status NOT IN ('done','failed','cancelled')"):
        for lb in json.loads(r["labels"] or "[]"):
            if isinstance(lb, str) and lb.startswith("auto_review:") and lb[12:].isdigit() and int(lb[12:]) in covered:
                out[int(lb[12:])] = r
    return out


def _take_over_dependents(db, old_id: int, new_id: int) -> None:
    """Re-point every open task waiting on `old_id` to `new_id`. One blocked only because that
    dependency died goes back to the queue; a block with another cause stays."""
    for t in _open_dependents(db, old_id):
        before = dependency_ids(t)
        deps = list(dict.fromkeys(new_id if d == old_id else d for d in before))
        upd: dict[str, Any] = {"depends_on": deps}
        if t["status"] == "blocked" and db.dead_dependency(before) and not db.dead_dependency(deps):
            upd.update(status="queued", blocked_reason=None)
        db.update_task(t["id"], **upd)


def dir_list(v: Any) -> list[str]:
    """Directories from a config value. The action schema carries `value` as a string, so a list
    arrives JSON-encoded or comma/newline separated, and an older config may hold such a string
    as a single list element. A path may itself contain a comma, so a piece that is an existing
    directory is kept whole; `existing_dirs` rejects whatever a split got wrong."""
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
        out += _split_dirs(text)
    return out


def _split_dirs(text: str) -> list[str]:
    out: list[str] = []
    for part in [text] if _is_dir(text) else re.split(f"[\n{re.escape(os.pathsep)}]", text):
        part = part.strip()
        if part:
            out += [part] if _is_dir(part) else [x.strip() for x in part.split(",") if x.strip()]
    return out


def _is_dir(path: str) -> bool:
    return bool(path) and Path(os.path.expanduser(path)).is_dir()


MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
# A tool workers may not call (providers.claude.disallowed_tools): a tool name such as
# mcp__<server>__<tool>, or a trailing * for every tool that starts so (mcp__<server>__*).
TOOL_PATTERN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,199}\*?$")


def effort_triggers(db, cfg: dict, event_ids: list[int], wake_due: str | None,
                    msg_ids: list[int] | None = None, gates: dict | None = None,
                    now: float | None = None, conflicts: list[dict] | None = None) -> tuple[list[str], dict]:
    """Why the coming coordinator turn is tricky or blocking, as trigger labels ([] for a routine
    one), and the state-based triggers' state for EFFORT_SEEN_KEY, saved once the turn starts so a
    lasting state raises one turn, not every turn. All the rules live here; each label names its
    rule, so turns can be counted by trigger. `conflicts`: the charter's contradicting Restrictions
    pairs (restriction_pairs); a pair not seen before raises the turn whose digest shows it."""
    now = now or time.time()
    c = cfg.get("coordinator") or {}
    out: list[str] = []
    seen_before = db.kv(EFFORT_SEEN_KEY, {}) or {}
    seen: dict = {}

    def add(label: str) -> None:
        if label not in out:
            out.append(label)
    rows = db.q(f"SELECT kind, severity, text, task FROM events WHERE id IN ({','.join('?' * len(event_ids))})",
                list(event_ids)) if event_ids else []
    for r in sorted(rows, key=lambda r: r["kind"]):
        if r["kind"] == "resource_trouble" and RESOURCE_WAITS_ONLY in (r["text"] or ""):
            continue
        if r["kind"] in EFFORT_EVENT_TRIGGERS:
            add(r["kind"])
    # A review that asked for changes is not a failure, but what to do with its findings is a decision.
    if any(r["kind"] in ("task_failed", "task_changes_needed") and r["task"]
           and (db.task(r["task"]) or {}).get("kind") == "review" for r in rows):
        add("failed review")
    # An area whose reviews keep failing across stacks needs a re-plan, not another fix round.
    if any(r["kind"] == reviewcap.REVIEW_AREA_EVENT for r in rows):
        add(reviewcap.TRIGGER)
    # Holds that wait on nothing anyone will act on (anchors.py): the daemon raises each new set once.
    if any(r["kind"] == anchors.STALE_EVENT for r in rows):
        add(anchors.TRIGGER)
    # A muted condition that outlasted its mute's escalate_after_h: is its recovery happening?
    if any(r["kind"] == "observation" and scr.MUTE_PERSISTS in (r["text"] or "") for r in rows):
        add("muted condition persists")
    if any(r["severity"] in EFFORT_SEVERITIES for r in rows):
        add("high severity event")
    if msg_ids:
        add("user message")
        texts = db.q(f"SELECT text FROM messages WHERE id IN ({','.join('?' * len(msg_ids))})", list(msg_ids))
        if any(CONFLICT_RE.search(m["text"] or "") for m in texts):
            add("change of plan")
    rejected = [str(x) for x in db.kv(REJECTED_KEY, []) or []]
    if any(x.startswith("ask_user:") for x in rejected):
        add("retry after a rejected ask_user")
    elif rejected:
        add("rejected action")
    last = float(db.kv("last_coordinator_turn", 0) or 0)
    if db.one("SELECT 1 FROM messages WHERE direction='out' AND kind='alert' AND ts>? AND severity IN (%s)"
              % ",".join("?" * len(EFFORT_SEVERITIES)), (last, *EFFORT_SEVERITIES)):
        add("high severity alert")
    # A task that keeps failing or coming back waiting, counted over 24 h for the tasks heard from
    # since the last turn. Waits: only external ones (unblock.counted_wait), and not routine waits on
    # live jobs or locks (more than live_waits_max on the same ones in 24 h, or a stalled job log, count).
    # They raise when the task's normalized wait reason changes, once when its current stint of waits
    # passes 24 h, and at repeat_waits_24h waits in 24 h as a backstop for real loops.
    fails_at, waits_at = int(c.get("repeat_fails_24h", 2) or 0), int(c.get("repeat_waits_24h", 8) or 0)
    since = now - 86400
    waits_seen = dict(seen_before.get("waits") or {})
    for t in db.q("SELECT DISTINCT task FROM events WHERE task IS NOT NULL AND ts>?", (last,)):
        fails = db.one("SELECT COUNT(*) n FROM runs WHERE task=? AND role!='coordinator' AND ended>? AND status IN "
                       f"({','.join('?' * len(machines.BAD_RUNS))}) AND COALESCE(note,'') NOT LIKE '%lost_to_reboot%'",
                       (t["task"], since, *machines.BAD_RUNS))["n"] + \
            db.one("SELECT COUNT(*) n FROM events WHERE task=? AND kind='task_failed' AND ts>?", (t["task"], since))["n"]
        if fails_at and fails >= fails_at:
            add("repeated failures")
        if wait_raises(db, t["task"], waits_seen, waits_at, now, live_max(cfg)):
            add("repeated waits")
    seen["waits"] = {k: v for k, v in waits_seen.items()   # a finished task's waits are over
                     if (db.task(int(k)) or {}).get("status") not in TERMINAL_TASK_STATES}
    # Free worker slots while every queued task is held (a dependency, a paused resource) or
    # deferred on purpose, at least one of them held: a queue of planned deferrals alone is routine
    # (deferral_expired, deferral_probe_broken and dead_dependency catch the ones that go wrong).
    # A task waiting only on queued deferrals (directly or down a chain) is deferred too. It raises
    # only when an id not held before joins the held set; a shrinking or unchanged set is routine.
    queued = db.q("SELECT * FROM tasks WHERE status='queued' ORDER BY id")
    if queued:
        paused = db.paused_resources()
        unmet = db.unmet_dependencies(queued)
        boxed = {t["id"] for t in queued if task_resources(t) & paused.keys()}
        deferred = {t["id"] for t in queued if t["id"] not in boxed and not unmet[t["id"]]
                    and ("when" in (d := deferral(t)) or float(d.get("after") or 0) > now)}
        grew = True
        while grew:
            more = {t["id"] for t in queued if t["id"] not in boxed | deferred and unmet[t["id"]]
                    and set(unmet[t["id"]]) <= deferred}
            deferred |= more
            grew = bool(more)
        held = [t["id"] for t in queued if t["id"] in boxed or (unmet[t["id"]] and t["id"] not in deferred)]
        before = set(seen_before.get("held") or [])
        running = db.one("SELECT COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running'")["n"]
        slots = int((cfg.get("budget") or {}).get("max_parallel_workers", 6) or 0)
        if held and running < slots and len(held) + len(deferred) == len(queued):
            if set(held) - before:
                add("idle slots, queued work held")
            seen["held"] = held
        elif before & set(held):   # still held while the slots were busy: not new when they free up
            seen["held"] = [x for x in held if x in before]
    keys = sorted(x["key"] for x in conflicts or [])
    seen["charter_conflicts"] = keys
    if set(keys) - set(seen_before.get("charter_conflicts") or []):
        add("charter conflict")
    # The budget gate going red or leaving it is a spend decision.
    red = sorted(k for k, g in (gates or {}).items() if (g or {}).get("level") == "red")
    seen["red"] = red
    if gates is not None and red != (seen_before.get("red") or []):
        add("budget gate change")
    if wake_due == "idle":
        if db.one("SELECT id FROM tasks WHERE status='blocked'"):
            add("stalled on blocked tasks")
        if db.one("SELECT id FROM messages WHERE kind='ask' AND handled=0"):
            add("stalled on open asks")
    skip = c.get("effort_skip_triggers") or []
    skip = {str(x) for x in skip} if isinstance(skip, list) else set()
    return [x for x in out if x not in skip], seen


def live_max(cfg: dict) -> int:
    """coordinator.live_waits_max: waits on the same live jobs or locks in 24 h that stay routine."""
    try:
        return max(0, int((cfg.get("coordinator") or {}).get("live_waits_max", unblock.LIVE_WAITS_MAX)))
    except (TypeError, ValueError):
        return unblock.LIVE_WAITS_MAX


def wait_raises(db, tid: int, waits_seen: dict, waits_at: int, now: float,
                live: int = unblock.LIVE_WAITS_MAX) -> bool:
    """Whether task `tid`'s external waits make a tricky turn, updating its entry in `waits_seen`
    ({reason, since, aged}): its normalized wait reason changed from its previous wait (the one
    seen by the last turn, else the previous in the stint), its stint (external waits in a row,
    the same wait's cheap wakes skipped; a self-wait or any other hand-off ends it) began over
    24 h ago (once per stint), or `waits_at` or more external waits in 24 h (0: no backstop)."""
    rows, stint = wait_stint(db, tid, now, live)
    key = str(tid)
    if not stint:
        waits_seen.pop(key, None)
        return False
    before = waits_seen.get(key) or {}
    reason, start = unblock.wait_reason(stint[0]), stint[-1]["ts"]
    if before.get("since") != start:
        before = {}   # a new stint: what an earlier one waited for does not compare
    prev = before.get("reason") or (unblock.wait_reason(stint[1]) if len(stint) > 1 else reason)
    aged = now - start > 86400
    waits_seen[key] = {"reason": reason, "since": start, "aged": aged}
    count = sum(1 for i, e in enumerate(rows) if e["ts"] > now - 86400 and e["kind"] == "task_waiting"
                and unblock.counted_wait(e, rows[i + 1:], live))
    return reason != prev or (aged and not before.get("aged")) or bool(waits_at and count >= waits_at)


def wait_stint(db, tid: int, now: float, live: int = unblock.LIVE_WAITS_MAX) -> tuple[list, list]:
    """Task `tid`'s recent hand-off events, newest first, and its current stint of counted waits
    (unblock.counted_wait, `live` its live_max; newest first; the same wait's cheap wakes skipped):
    empty when its last hand-off was no such wait."""
    rows = db.q("SELECT ts, kind, text, data FROM events WHERE task=? AND ts>? AND kind IN "
                f"({','.join('?' * len(STINT_KINDS))}) ORDER BY ts DESC, id DESC",
                (tid, now - STINT_LOOKBACK_S, *STINT_KINDS))
    stint = []
    for i, e in enumerate(rows):
        if e["kind"] == "task_waiting" and unblock.ESCALATED_WAKE.search(e["text"] or ""):
            continue
        if e["kind"] != "task_waiting" or not unblock.counted_wait(e, rows[i + 1:], live):
            break
        stint.append(e)
    return rows, stint


def can_raise_effort(cfg: dict, tier: str, effort: str | None = None) -> bool:
    """Whether a tricky turn would run at more than the coordinator's base `effort` (by default its
    tier's): not when coordinator.effort pins it or coordinator.unblock_effort is empty."""
    c = cfg.get("coordinator") or {}
    floor = str(c.get("unblock_effort", "high") or "")
    if str(c.get("effort") or "") or not floor:
        return False
    if effort is None:
        effort = str(((cfg.get("providers") or {}).get(cfg.get("core_provider", "claude")) or {})
                      .get("tiers", {}).get(tier, {}).get("effort", "") or "")
    return raise_effort(effort, floor) != effort


def raise_effort(effort: str, floor: str) -> str:
    """`effort`, raised to at least `floor`; an empty floor leaves it as is."""
    if not floor or (effort in EFFORT_ORDER and floor in EFFORT_ORDER
                     and EFFORT_ORDER.index(effort) >= EFFORT_ORDER.index(floor)):
        return effort
    return floor


def name_list(v: Any, strict: bool = False, pattern: re.Pattern = MCP_NAME_RE,
              what: str = "an MCP server name") -> list[str]:
    """Names from a config value: a list, a JSON-encoded list or a comma/newline-separated string."""
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.strip().startswith("[") else v
        except ValueError:
            pass
    items = v if isinstance(v, list) else re.split(r"[,\n]", str(v))
    out = list(dict.fromkeys(str(x).strip() for x in items if str(x).strip()))
    bad = [n for n in out if not pattern.match(n)]
    if strict and bad:
        raise ValueError(f"not {what}: {', '.join(bad)}; nothing was changed")
    return [n for n in out if n not in bad]


def tool_list(v: Any, strict: bool = False) -> list[str]:
    """Tool names or patterns from a config value, as name_list reads server names."""
    return name_list(v, strict, TOOL_PATTERN_RE, "a tool name or pattern (e.g. mcp__server__tool or "
                     "mcp__server__*)")


def existing_dirs(dirs: list[str]) -> list[str]:
    missing = [d for d in dirs if not _is_dir(d)]
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
            db.post("out", f"No answer to ask {k} after {hours:g}h, so I went with the recommendation: {rec}\n"
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


RESOURCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,79}")
PUSH_LOCK_RE = re.compile(r"push:[A-Za-z0-9_.@+%-]{1,200}")   # the lock `ttp push` holds (push.py)


def push_lock_name(name: str) -> str | None:
    """The canonical lock name of `push:<remote>/<branch>` (or its %2F-encoded form), the one
    `ttp push` holds, so a task can wait for a push's turn; None if `name` is not one."""
    if not isinstance(name, str) or not name.startswith("push:"):
        return None
    from urllib.parse import quote, unquote
    canon = "push:" + quote(unquote(name[5:].strip()), safe="")
    return canon if PUSH_LOCK_RE.fullmatch(canon) else None


def _resource_names(names, action: str, problems: list) -> list[str]:
    """The valid resource names; each dropped one is reported, so a typo is not silently lost."""
    ok = []
    for r in names if isinstance(names, list) else [names]:
        if isinstance(r, str) and RESOURCE_RE.fullmatch(r):
            ok.append(r)
        elif push_lock_name(r):
            ok.append(push_lock_name(r))
        else:
            problems.append(f"{action}: resource {r!r} dropped: names are letters, digits and _.@+- "
                            f"(max 80, starting with a letter or digit), or a push lock push:<remote>/<branch>")
    return ok


def pause_resource(p: Project, name: str, paused: bool, reason: str = "", by: str = "user",
                   key: str | None = None, db=None, end: dict | None = None) -> str:
    """Pause or resume one resource for the project's tasks. While paused, no task labelled with it
    is dispatched and `ttp lock` refuses it; running workers whose task uses it are told mid-run.
    Resuming it makes tasks that handed off `waiting` on the pause due now. A shared resource
    (shared.py) is paused for every project that names it; their daemons tell their own workers
    (sync_shared_pauses). `db` is the caller's own connection when it runs on another thread (the
    web app). `end` (pauseends.from_action, which the action, `ttp pause` and the web app require)
    replaces the pause's end; left out, a pause keeps the end it has. Returns a line for the user."""
    name = (name or "").strip()
    if not RESOURCE_RE.fullmatch(name):
        raise ValueError(f"not a resource name: {name!r}")
    db, woken = db or p.db, 0
    sync_shared_pauses(p, db)   # first keep a pause of a resource no longer shared as the project's own
    is_shared = shared.is_shared(p, name)
    reason = " ".join(str(reason or "").split())[:300]

    def _entry(was: dict | None) -> dict:
        was = was or {}
        kept = end if end is not None else {k: was[k] for k in pauseends.END_FIELDS if k in was}
        return {**kept, "reason": reason or was.get("reason", ""), "since": was.get("since") or time.time(),
                # The coordinator may lift only a pause the user had no part in.
                "by": "user" if "user" in (by, was.get("by")) else by,
                **({"project": p.name} if is_shared else {})}

    with db.tx():
        cur = db.paused_resources(shared=False)
        seen = db.kv(SHARED_SEEN_KEY) or {}
        if paused:
            if is_shared:
                entry = shared.update_pause(name, lambda w: _entry(w or cur.get(name)))[1]
                cur.pop(name, None)
                seen[name] = entry
            else:
                entry = cur[name] = _entry(cur.get(name))
        else:
            was = shared.update_pause(name, lambda w: None)[0] if is_shared else None
            if name not in cur and was is None:
                return f"{name} is not paused"
            since = min(float(x.get("since") or 0) for x in (cur.get(name), was) if x)
            cur.pop(name, None)
            seen.pop(name, None)
            woken = _wake_pause_waiters(db, name, since)
        db.set_kv(PAUSED_RESOURCES_KEY, cur)
        if is_shared:
            db.set_kv(SHARED_SEEN_KEY, seen)
    why = f" ({entry['reason']})" if paused and entry["reason"] else ""
    _tell_runs(db, name, paused, why, key)
    scope = " for every project that shares it" if is_shared else ""
    till = f"; {pauseends.describe(entry)}" if paused and pauseends.describe(entry) else ""
    return (f"{name} paused{why}{scope}: tasks using it wait, and `ttp lock {name}` refuses it{till}" if paused
            else f"{name} resumed{scope}" + (f"; {woken} task(s) that waited on it start again" if woken else ""))


def _tell_runs(db, name: str, paused: bool, why: str = "", key: str | None = None) -> None:
    """Tell the running workers whose task uses the resource that it was paused or resumed."""
    text = (f"The resource `{name}` is paused{why}. Do not use it: start no new command on it, and `ttp lock "
            f"{name}` refuses it. Finish or stop what already runs on it safely; if the task cannot go on "
            f"without it, save your work and hand off `waiting` naming `{name}`. The task is dispatched "
            f"again once the pause is lifted." if paused else
            f"The resource `{name}` is no longer paused; you may use it again (through `ttp lock {name}`).")
    for r in db.q("SELECT r.dir, t.labels FROM runs r JOIN tasks t ON t.id=r.task "
                  "WHERE r.status='running' AND r.role!='coordinator'"):
        if r["dir"] and Path(r["dir"]).is_dir() and name in task_resources({"labels": r["labels"]}):
            _append_update(Path(r["dir"], "steer.md"), text, key)


def sync_shared_pauses(p: Project, db=None) -> None:
    """Act on pauses of shared resources set or lifted from another project: tell this project's
    workers, and on a resume make the tasks that waited on it due. Each change is acted on once.
    A resource that stopped being shared while paused is not resumed: its pause is kept as the
    project's own, `by` and all, and nothing is woken. Then record the project's slot counts."""
    db = db or p.db
    cfg = p.config()
    mine = shared.names(cfg)
    cur = shared.paused(cfg)
    seen = {k: v if isinstance(v, dict) else {"since": v} for k, v in (db.kv(SHARED_SEEN_KEY) or {}).items()}
    left = (set(seen) | shared.recorded(p)) - mine
    if cur != seen or left:
        for name, v in cur.items():
            if (seen.get(name) or {}).get("since") != v.get("since"):
                _tell_runs(db, name, True, f" ({v['reason']})" if v.get("reason") else "")
        for name in sorted((set(seen) | left) - set(cur)):
            still = shared.read_pause(name) if name in left else None
            if still is not None:
                with db.tx():
                    local = db.paused_resources(shared=False)
                    was = local.get(name) or {}
                    local[name] = {**{k: still[k] for k in pauseends.END_FIELDS if k in still},
                                   "reason": still.get("reason") or was.get("reason", ""),
                                   "since": was.get("since") or still.get("since") or time.time(),
                                   "by": "user" if "user" in (still.get("by"), was.get("by"))
                                   else still.get("by") or "user"}
                    db.set_kv(PAUSED_RESOURCES_KEY, local)
            elif name in seen:
                _wake_pause_waiters(db, name, float(seen[name].get("since") or 0))
                _tell_runs(db, name, False)
        db.set_kv(SHARED_SEEN_KEY, cur)
    shared.record_slots(p, cfg)


def _wake_pause_waiters(db, name: str, since: float) -> int:
    """Tasks that handed off `waiting` on `name` while it was paused are due now: their wait was the
    pause, not their `retry_after_s` timer or `retry_when` probe. Returns how many woke."""
    woken = 0
    for t in db.q("SELECT * FROM tasks WHERE status='queued' AND not_before IS NOT NULL"):
        prev = load_result(t["result"])
        at = prev.get("waiting_since")
        at = at if isinstance(at, (int, float)) else t["updated"] or 0
        said = f"{prev.get('waiting_for') or ''} {prev.get('summary') or ''}"
        if prev.get("status") != "waiting" or at < since or name not in task_resources(t) \
                or not re.search(rf"(?<![\w.@+-]){re.escape(name)}(?![\w@+-])", said):
            continue
        db.update_task(t["id"], not_before=None, blocked_reason=None,
                       result=dump_result({**prev, "woke": f"the resource {name} was resumed"}))
        woken += 1
    return woken


def task_resources(task: dict) -> set[str]:
    """Resources a task names in its labels, shared (`resource:`) or held for the run (`exclusive:`)."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except ValueError:
        return set()
    return {lb.split(":", 1)[1] for lb in labels if isinstance(lb, str)
            and lb.split(":", 1)[0] in ("resource", "exclusive") and ":" in lb}


MEMORY_ALERT_KEY = "memory_pinned_over"   # kv: set once pinned memory alone has outgrown the budget


def memory_budget_line(p: Project) -> str:
    """One digest line once memory no longer fits whole: what each prompt leaves out."""
    _, coord = p.memory_select(COORDINATOR_MEMORY_CHARS)
    _, work = p.memory_select(WORKER_MEMORY_CHARS)
    if work["shown"] >= work["entries"]:
        return ""
    return (f"## Memory over budget: {coord['entries']} entries, {coord['chars']} chars; you see "
            f"{coord['shown']}, workers see {work['shown']} (pinned restrictions/preferences/resources "
            f"{coord['pinned_chars']} chars, always shown). Retire stale entries with `memory_forget`, "
            f"or `supersedes` when a new one replaces them; a (standing) entry is only shortened (a "
            f"standing `memory_add` that supersedes it), never retired for space")


def memory_budget_check(p: Project) -> None:
    """Tell the coordinator once when pinned memory alone no longer fits the workers' budget; it
    is shown whole anyway, at the cost of every decision and fact. Re-arms once it fits again."""
    over = p.memory_select(WORKER_MEMORY_CHARS)[1]
    flagged = p.db.kv(MEMORY_ALERT_KEY)
    if over["pinned_over"] and not flagged:
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
               (time.time(), "daemon", "memory_over_budget", "normal",
                f"Pinned memory ({over['pinned']} restriction/preference/resource entries, "
                f"{over['pinned_chars']} chars) alone exceeds the workers' {over['limit']}-char memory "
                f"budget, so they get no decisions or facts. Merge or retire pinned entries "
                f"(`memory_add` with `supersedes`, or `memory_forget`). Shorten (standing) entries "
                f"with a standing `memory_add` that supersedes them; never retire them for space.", "queued"))
        p.db.set_kv(MEMORY_ALERT_KEY, True)
    elif flagged and not over["pinned_over"]:
        p.db.set_kv(MEMORY_ALERT_KEY, False)


def _hours_or_none(a: dict, key: str) -> float | None:
    """schedule_set's `key`: hours (a number, at least 0) or null."""
    v = a.get(key)
    if v is None:
        return None
    try:
        hours = float(v)
    except (TypeError, ValueError):
        hours = -1.0
    if hours < 0 or hours != hours:
        raise ValueError(f"schedule_set {a.get('name')!r} rejected: `{key}` must be hours (a number, 0 or more) or null")
    return hours


def _tell_running_workers(db, text: str, key: str | None = None) -> None:
    """A new restriction binds work already in flight, not only work started later: it goes to
    every running worker's update file, which the worker receives mid-run."""
    for r in db.q("SELECT dir FROM runs WHERE status='running' AND role!='coordinator'"):
        if r["dir"] and Path(r["dir"]).is_dir():
            _append_update(Path(r["dir"], "steer.md"), text, key)


# Words by which a new rule allows what it names, or narrows an old one ("may", "no longer", "except").
_LOOSEN_RE = re.compile(r"\b(may|allow(s|ed|ing)?|permit(s|ted)?|fine to|ok(ay)? to|no longer|lift(s|ed)?|"
                        r"except|exceptions?|unless|instead|can now|relax(es|ed)?|widen(s|ed)?)\b", re.I)
# Rule and filler words that say nothing about what a restriction is about.
_RULE_STOP = {"never", "always", "only", "must", "should", "would", "could", "with", "without", "from", "that",
              "this", "these", "those", "when", "while", "them", "they", "their", "there", "then", "than", "each",
              "every", "other", "into", "onto", "also", "unless", "except", "allow", "allowed", "permitted", "will",
              "been", "being", "have", "does", "done", "after", "before", "until", "about", "what", "which",
              "your", "more", "less", "some", "first", "even", "again", "still", "longer", "instead", "now"}


def _stem(word: str) -> str:
    for end in ("ing", "ed", "es", "s", "e"):
        if word.endswith(end) and len(word) - len(end) >= 3:
            return word[:-len(end)]
    return word


def _rule_words(text: str) -> set[str]:
    """What a rule is about: its longer words, stemmed, rule and filler words left out."""
    return {_stem(w) for w in re.findall(r"[a-z][a-z0-9-]*[a-z0-9]", text.lower())
            if len(w) >= 4 and w not in _RULE_STOP}


def _widens(item: str, words: set[str]) -> bool:
    """Whether a loosening sentence with these rule words is about the restriction `item`: they share
    at least two words, and at least half of the item's."""
    about = _rule_words(item)
    shared = about & words
    return len(shared) >= 2 and 2 * len(shared) >= len(about)


def _sentences(lines) -> list[str]:
    return [x.strip(" -*") for line in lines for x in re.split(r"(?<=[.;!?])\s+", line.strip()) if x.strip(" -*")]


def _restriction_conflicts(p: Project, before: str, added: list[tuple[str, str]]) -> list[str]:
    """Warn when a user turn added text that allows or narrows what an older restriction is about
    while no charter_update in it edited Restrictions (`quote` or `replaces`): the old item still
    stands, and workers obey it as binding. A warning in the next digest (a queued event) for what
    the guard (_reject_contradicting_append) let through on wording alone; text sent with
    `both_hold` is not warned about. A new rule with no allowing word, or about something no
    restriction names, adds a restriction and changes nothing: no warning. Returns the warnings."""
    from .prompts import charter_restrictions
    now = " ".join(charter_restrictions(p.charter_path.read_text()).split())
    olds = _sentences(before.splitlines())
    out = []
    for target, text in added:
        if not _LOOSEN_RE.search(text):
            continue
        words = _rule_words(text)
        for old in olds:
            if _widens(old, words) and " ".join(old.split()) in now:
                out.append(f"The user's turn added to {target!r}: \"{clip(text, 200)}\", but the Restrictions "
                           f"item \"{clip(old, 200)}\" still stands unchanged, and workers obey it as binding. "
                           f"If the user changed that restriction, charter_update section Restrictions with "
                           f"`quote` set to that item, `text` the new wording and `over` naming the user's message "
                           f"that changed it. If both truly hold, leave them.")
                break
    for x in out:
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
               (time.time(), "harness", "charter_conflict", "normal", x, "queued"))
    return out


CHARTER_LINT_KEY = "charter_lint"   # kv: {"stat", "hash"} of the charter last linted, "seen" pair keys flagged
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# A Restrictions item that already points at the section holding its exception has been reconciled.
_POINTS_AT_EXCEPTION_RE = re.compile(r"\b(except|unless)\b.*\bsections?\b", re.I)
# What makes a Restrictions item a limit; an item without one (a note, a record) forbids nothing.
_PROHIBITS_RE = re.compile(r"\b(never|no|not|only|must|always|keep|stay|avoid|without|forbid\w*|prohibit\w*)\b|n't\b", re.I)
# A limiting word in a clause of a later sentence ("broker only", "never directly").
_LIMIT_RE = re.compile(r"\b(never|only|not|no(?! longer\b))\b|n't\b", re.I)


def _restates_limit(item: str, sentence: str) -> bool:
    """Whether a loosening sentence keeps the item's own limit instead of widening it: one of its
    clauses carries a limiting word, and no loosening one, next to a word the item is about
    ("X (broker only)", but not "X is allowed only for Y"), or it
    allows something within what the item's "only ..." still permits ("jobs may queue through each
    broker" against "jobs only through each broker")."""
    about = _rule_words(item)
    if any(_LIMIT_RE.search(c) and not _LOOSEN_RE.search(c) and _rule_words(c) & about
           for c in re.split(r"[,;:()]", sentence)):
        return True
    m = re.search(r"\bonly\b([^,;:()]*)", item, re.I)
    limit = _rule_words(m.group(1)) if m else set()
    return bool(limit) and 2 * len(limit & _rule_words(sentence)) >= len(limit)


def charter_lint(p: Project) -> list[str]:
    """Whole-charter check, run on the daemon tick whenever the charter's hash changes: a dated
    `## ...` section (heading with a date, e.g. "(user, 2026-01-02)") that allows or narrows what an
    earlier Restrictions item is about, while that item still stands. Workers obey the item as
    binding, so the later section does nothing. _restriction_conflicts catches this only when a
    coordinator charter_update adds the text in a user turn; a section edited in by hand, or added
    before that check, is caught here. Same wording rule, made stricter: items that limit nothing,
    items dated after the section, Resources sections and sentences that keep the item's own limit
    are left alone. One queued charter_conflict event per
    (item, section) pair ever; the charter is never edited. No model. Returns the new warnings."""
    from .prompts import charter_sections
    path = p.charter_path
    try:
        st = path.stat()
    except OSError:
        return []
    state = p.db.kv(CHARTER_LINT_KEY) or {}
    stat = [st.st_mtime_ns, st.st_size]
    if state.get("stat") == stat:
        return []
    text = path.read_text()
    digest = hashlib.sha256(text.encode()).hexdigest()
    state["stat"] = stat
    if state.get("hash") == digest:
        p.db.set_kv(CHARTER_LINT_KEY, state)
        return []
    seen = set(state.get("seen") or [])
    items: list[tuple[str, str]] = []   # (item, latest date on it or its heading): Restrictions above this section
    out = []
    for heading, body in charter_sections(text):
        name = heading[3:].strip()
        if name.lower().startswith("restriction"):
            items += [(x, max(_DATE_RE.findall(f"{name} {x}"), default="")) for x in _sentences(body)
                      if not (x.startswith("(") and x.endswith(")")) and _PROHIBITS_RE.search(x)]
            continue
        # A Resources section lists what may be used, under the restrictions' own terms.
        dated = max(_DATE_RE.findall(name), default="")
        if not heading or not items or not dated or name.lower().startswith("resource"):
            continue
        for sentence in _sentences(body):
            if not _LOOSEN_RE.search(sentence):
                continue
            words = _rule_words(sentence)
            for item, since in items:
                # An item dated after the section is the newer word, whatever the file order.
                if (since > dated or _POINTS_AT_EXCEPTION_RE.search(item) or not _widens(item, words)
                        or _restates_limit(item, sentence)):
                    continue
                key = hashlib.sha256(f"{' '.join(item.split())}\n{' '.join(name.split())}".encode()).hexdigest()[:16]
                if key in seen:
                    continue
                seen.add(key)
                out.append(f"The charter section {name!r} says \"{clip(sentence, 200)}\", but the Restrictions "
                           f"item \"{clip(item, 200)}\" still stands unchanged, and workers obey it as binding. "
                           f"If the user changed that restriction, charter_update section Restrictions with "
                           f"`quote` set to that item and `text` the new wording (it may point at the dated "
                           f"section for the exception). If both truly hold, leave them.")
    for x in out:
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
               (time.time(), "harness", "charter_conflict", "normal", x, "queued"))
    state.update(hash=digest, seen=sorted(seen))
    p.db.set_kv(CHARTER_LINT_KEY, state)
    return out


# Contradicting Restrictions items (restriction_pairs, _append_conflicts), by one coarse rule
# (_overlaps): two rules overlap when they share what they do or what to, whatever their stance,
# condition or scope; the lint takes a stricter reading of it. A false hit costs one resend (guard)
# or one acknowledgement by key (lint, settle_conflicts); a miss leaves a stale ban that workers obey.
# A word that bans, or one that permits, anywhere in a sentence, for the lint's report: negations,
# conditions and scopes are not read.
_BAN_RE = re.compile(r"\b(?:never|not|no|nobody|none|only|must|cannot|without|forbid\w*|prohibit\w*|"
                     r"ban(?:s|ned)?)\b|n['’]t\b", re.I)
_PERMIT_RE = re.compile(r"\b(?:allow\w*|permit\w*|may|can|fine|ok(?:ay)?|lift\w*|no longer|"
                        r"no (?:problem|objection|issue)s?|objects|minds|stops)\b", re.I)
# An item that names its own exception ("except as the dated section allows") has been reconciled.
_OWN_EXCEPTION_RE = re.compile(r"\b(?:except|unless|other than|apart from|save for|excluding)\b", re.I)
# Actions, by what they do. A generic verb ("touch", "modify", "use") covers every action (_ANY); a
# write covers every action that changes something (_WRITES); two specific ones conflict only when
# they are the same.
_ACTIONS = {"push": {"push"}, "force-push": {"push"}, "modify": {"any"}, "modifi": {"any"}, "edit": {"write"},
            "touch": {"any"}, "alter": {"any"}, "disturb": {"any"}, "use": {"any"}, "using": {"any"},
            "used": {"any"}, "writ": {"write"}, "rewrit": {"write"}, "commit": {"write"},
            "merg": {"merge"}, "delet": {"delete"}, "remov": {"delete"},
            "open": {"open"}, "creat": {"open"}, "deploy": {"deploy"}, "publish": {"deploy"},
            "install": {"install"}, "upgrad": {"install"}, "run": {"run"}, "runn": {"run"}, "start": {"run"},
            "submit": {"run"}, "restart": {"stop"}, "reboot": {"stop"}, "kill": {"stop"}, "cancel": {"stop"},
            "stop": {"stop"}}
_ANY = "any"
_WRITES = {"write", "push", "merge", "delete"}
# Words that are no target: rule, stance, actor and filler words, and verbs that let someone act.
_NOT_TARGET = {x for w in _RULE_STOP | {
    "the", "and", "for", "nor", "not", "may", "can", "its", "own", "are", "was", "has", "had", "all", "but", "any",
    "you", "our", "per", "via", "too", "yet", "one", "off", "keep", "stay", "avoid", "permitt", "fine", "okay",
    "lift", "forbidden", "prohibit", "bann", "off-limits", "problem", "objection", "objects", "minds", "stops",
    "nobody", "none", "anyone", "everyone", "someone", "worker", "agent", "task", "user", "owner", "maintainer",
    "human", "admin", "operator", "coordinator", "reviewer", "let", "tell", "ask", "make", "get", "enable",
    "instruct", "change", "work", "thing", "anything", "something", "everything", "explicit", "word", "directly",
    "ever", "pull", "request", "prs"} for x in (w, _stem(w))}
# Kinds of target: "the main branch" and "the release branch" share only a kind, which is no overlap.
_KINDS = {_stem(w) for w in ("branch", "repo", "repository", "folder", "directory", "file", "path", "box",
                             "machine", "node", "host", "server", "service", "project")}


def _same_action(a: set[str], b: set[str]) -> bool:
    return bool(a and b and (a & b or _ANY in a | b or "write" in a and b & _WRITES or "write" in b and a & _WRITES))


# A condition clause ("once CI passes", "after review"), up to the next comma: without an action of its
# own it says when, not what to ("if workers push to main" does say what to).
_CONDITION_RE = re.compile(r"\b(?:once|if|when|whenever|after|before|until|unless|provided|while|as soon as|"
                           r"as long as)\b[^,;:]*", re.I)


def _target(sentence: str, conditions: bool = True) -> tuple[set[str], set[str]]:
    """A rule's (action families, object words), from every word in it: a pull request is opened.
    Without `conditions`, words of a condition clause name no object (its actions still count)."""
    low = sentence.lower()
    pr = re.search(r"\bprs?\b|\bpull requests?\b", low)
    acts, objs = ({"open"}, {"pr"}) if pr else (set(), set())
    what = low if conditions else _CONDITION_RE.sub(
        lambda m: m[0] if any(_ACTIONS.get(_stem(w)) or _ACTIONS.get(w)
                              for w in re.findall(r"[a-z][a-z0-9-]*[a-z0-9]", m[0])) else " ", low)
    for w in re.findall(r"[a-z][a-z0-9-]*[a-z0-9]", low):
        if fam := _ACTIONS.get(_stem(w)) or _ACTIONS.get(w):
            acts |= fam
    for w in re.findall(r"[a-z][a-z0-9-]*[a-z0-9]", what):
        s = _stem(w)
        if not (_ACTIONS.get(s) or _ACTIONS.get(w)) and len(w) > 2 and w not in _NOT_TARGET and s not in _NOT_TARGET:
            objs.add(s)
    return acts, objs


def _overlaps(a: str, b: str, strict: bool = False) -> bool:
    """Whether two rules are about the same thing. For the guard (wide, a false hit costs one
    resend): they name the same action (_same_action) whatever its objects or conditions, or share
    an object word whatever their actions; sharing only a kind of target ("branch") counts only when
    one of them names nothing else ("Deploying to box A" twice). For the lint (`strict`, a pair stays
    in the digest until settled by key): they name the same action (_same_action), and they share an
    object word the same way, or one names no object outside its conditions (a rule on the action
    alone, "Pushing is fine once CI passes", covers every object)."""
    aa, ao = _target(a, conditions=not strict)
    ba, bo = _target(b, conditions=not strict)
    shared = ao & bo
    on_object = bool(shared - _KINDS or shared and not (ao - _KINDS and bo - _KINDS))
    if strict:
        return _same_action(aa, ba) and (not ao or not bo or on_object)
    return _same_action(aa, ba) or on_object


def _restriction_items(charter: str) -> list[tuple[str, str, int]]:
    """Every sentence of every Restrictions section (dated and temporary ones too), as (sentence,
    section name, item number): sentences of one bullet or paragraph share the number."""
    from .prompts import charter_sections
    out: list[tuple[str, str, int]] = []
    item = 0
    for heading, body in charter_sections(charter):
        name = " ".join(heading[3:].split())
        if not name.lower().startswith("restriction"):
            continue
        cur: list[str] = []
        for line in body + [""]:
            bullet = re.match(r"\s*(?:[-*+]|\d+[.)])\s", line)
            if (bullet or not line.strip()) and cur:
                par = " ".join(" ".join(cur).split())
                if not (par.startswith("(") and par.endswith(")")):
                    item += 1
                    out += [(x, name, item) for x in _sentences([par])]
                cur = []
            if line.strip():
                cur.append(line.strip())
    return out


def restriction_pairs(charter: str) -> list[dict]:
    """Model-free lint: pairs of Restrictions items, across every Restrictions section, where one has
    a word that bans (_BAN_RE) and the other one that permits (_PERMIT_RE), about the same thing
    (_overlaps), whatever their conditions or scopes. Workers obey both as binding, so the stricter one
    wins and the permitting one may do nothing. Each pair once, with a stable `key`; sentences of one
    item are never paired (an item may state its own exception in a second sentence), nor is a ban
    that names its own exception (_OWN_EXCEPTION_RE)."""
    items = _restriction_items(charter)
    out, seen = [], set()
    for f, fsec, fn in items:
        if not _BAN_RE.search(f) or _OWN_EXCEPTION_RE.search(f):
            continue
        for a, asec, an in items:
            if (an == fn or not _PERMIT_RE.search(a) or frozenset((f, a)) in seen
                    or not _overlaps(f, a, strict=True)):
                continue
            seen.add(frozenset((f, a)))
            key = hashlib.sha256(f"{' '.join(f.split())}\n{' '.join(a.split())}".encode()).hexdigest()[:12]
            out.append({"forbid": f, "forbid_section": fsec, "allow": a, "allow_section": asec, "key": key})
    return out


CHARTER_SETTLED_KEY = "charter_conflicts_settled"   # kv: pair keys (restriction_pairs) settled as both holding


def charter_conflicts(p: Project) -> list[dict]:
    """The charter's contradicting Restrictions pairs, less those the coordinator settled as both
    holding (settle_conflicts): those leave the digest, the daily review and the effort trigger."""
    try:
        pairs = restriction_pairs(p.charter_path.read_text())
    except OSError:
        return []
    settled = set(p.db.kv(CHARTER_SETTLED_KEY) or [])
    return [x for x in pairs if x["key"] not in settled]


def settle_conflicts(p: Project, keys: str) -> str:
    """Record Charter conflicts pairs, by `key`, as both holding (not a real contradiction): no
    charter edit, no ask. A pair whose item changes gets a new key and shows again; keys of pairs
    no longer in the charter are dropped."""
    want = set(re.findall(r"\b[0-9a-f]{12}\b", keys.lower()))
    if not want:
        raise ValueError("charter_update `both_hold` without text: `key` names no pair key (12 hex digits, "
                         "as the Charter conflicts digest section lists them)")
    try:
        current = {x["key"] for x in restriction_pairs(p.charter_path.read_text())}
    except OSError:
        current = set()
    known = want & current
    if known:
        p.db.set_kv(CHARTER_SETTLED_KEY, sorted(set(p.db.kv(CHARTER_SETTLED_KEY) or []) & current | known))
    if want - current:
        raise ValueError(f"charter_update `both_hold`: no current Charter conflicts pair has key "
                         f"{', '.join(sorted(want - current))}" + (f" (settled {', '.join(sorted(known))})"
                                                                    if known else ""))
    return f"charter conflicts settled as both holding: {', '.join(sorted(known))}"


def charter_conflict_lines(pairs: list[dict]) -> list[str]:
    """The digest's `## Charter conflicts` section (also listed in the daily review)."""
    if not pairs:
        return []
    return (["## Charter conflicts (Restrictions items that may contradict each other; workers obey both, so the "
             "stricter wins). Most pairs only share a word, or one is the other's limit or condition: settle each "
             "such pair with charter_update `both_hold`: true and `key` set to its key (no text; no charter edit, "
             "no ask). Only if the user's newer word replaced one side, charter_update its section with `quote` "
             "set to it, `text` what still holds (empty to drop it) and `over` naming that word. Never drop a "
             "restriction the user did not change."]
            + [f"- [{x['key']}] \"{clip(x['forbid'], 200)}\" ({x['forbid_section']}) vs \"{clip(x['allow'], 200)}\" "
               f"({x['allow_section']})" for x in pairs])


def _append_conflicts(charter: str, section: str, text: str, quote: str = "",
                      lifts_only: bool = False) -> list[tuple[str, str]]:
    """The standing Restrictions items that text added to Restrictions (dated or temporary too), Goals
    or Policies is about, as (item, its section): every item one of its sentences overlaps
    (_overlaps), whatever either one says about it (a lift, a new ban, a condition, a scope). An item
    the text replaces (`quote`) or that names its own exception (_OWN_EXCEPTION_RE) is left out. A
    false hit costs one resend (`quote`, or `both_hold`); a miss leaves a stale ban standing, and
    workers obey it. `lifts_only`: only sentences that may permit or loosen something (_may_lift)."""
    if not _DATED.sub("", section).lower().startswith(("restriction", "goal", "polic")):
        return []
    new, gone = _sentences(text.splitlines()), _loose(quote)
    if lifts_only:
        new = [x for x in new if _may_lift(x)]
    out: list[tuple[str, str]] = []
    for item, sec, _ in _restriction_items(charter):
        if ((item, sec) not in out and not (gone and _loose(item) in gone)
                and not _OWN_EXCEPTION_RE.search(item) and any(_overlaps(item, s) for s in new)):
            out.append((item, sec))
    return out


def _may_lift(sentence: str) -> bool:
    """Whether a sentence has a word that permits or loosens ("may", "allowed", "except", "no
    longer"): it may lift a ban it overlaps. A duty, a goal or a new ban has none."""
    return bool(_PERMIT_RE.search(sentence) or _LOOSEN_RE.search(sentence))


GUARD_REJECTED = "the old Restrictions item it contradicts still stood"   # _record_charter_approval `failed`


def _reject_contradicting_append(p: Project, section: str, text: str, user_turn: bool,
                                 messages: list[int] | None, ok: dict | None, quote: str = "",
                                 end: dict | None = None) -> str:
    """Refuse a charter_update that adds `text` (appended, or in place of `quote`) while a Restrictions
    item it is about would stay standing (_append_conflicts): workers obey that item as binding, so the
    change may do nothing. The rejection quotes the item. With the user's word behind the change (a
    user turn, or the approval it already carried) only a sentence that may lift what an item bans
    (_may_lift) is refused: any other overlap is a duty, a goal or a ban added next to the item, both
    hold, and the change goes through; the returned note names the items judged to overlap. A refused
    change of the user's is recorded (_record_charter_approval), as sent and as the quoted resend of
    each item, so either fix goes through in a later turn without asking again; it is never raised
    again by itself (reraise_charter_changes). Text already in the charter (a retried turn) passes.
    Returns the note, or ""."""
    charter = p.charter_path.read_text()
    if " ".join(text.split()) in " ".join(charter.split()):
        return ""
    hits = _append_conflicts(charter, section, text, quote)
    if not hits:
        return ""
    said = list(messages or []) if user_turn else list((ok or {}).get("messages") or [])
    if user_turn or ok is not None:
        lifts = _append_conflicts(charter, section, text, quote, lifts_only=True)
        if not lifts:
            return (f"charter_update: {clip(text, 120)!r} went in on the user's word, taken as holding alongside "
                    "the Restrictions item " + "; ".join(f"\"{clip(i, 160)}\"" for i, _ in hits)
                    + " (it only shares words with them; workers obey both). If the user changed one of them, "
                      "charter_update its section with `quote` set to it")
        hits = lifts
    if user_turn:
        _record_charter_approval(p, said, section, quote, "", text, end, "the charter guard: " + GUARD_REJECTED)
    for item, sec in hits:
        for name in dict.fromkeys([sec, _DATED.sub("", sec)]):
            _record_charter_approval(p, said, name, item, "", text, None, GUARD_REJECTED)
    first = hits[0]
    raise ValueError(
        f"charter_update: {clip(text, 200)!r} contradicts the Restrictions item "
        + "; ".join(f"\"{clip(i, 200)}\" (section {s!r})" for i, s in hits)
        + ", which would stay standing, and workers obey it as binding. Pick by what the user meant, two "
          "equal fixes: if this text replaces that item, resend with section "
          f"{first[1]!r}, `quote` \"{first[0]}\" and this `text`"
        + (" (the user's yes is on record for it)" if said else ", with `over` naming the user's word that changed it")
        + (", and retire the other items with `quote` and `over` too" if len(hits) > 1 else "")
        + "; if both hold (this text only adds a limit, a ban or a condition, is the item's "
          "exception, or is not about the same thing), resend "
          "with `both_hold`: true, or rewrite the item to name its exception. Never drop a restriction the "
          "user did not change")


def _append_update(steer: Path, text: str, key: str | None = None) -> None:
    """Add an update to a run's steer.md, once per `key` (see apply)."""
    if key and _has_line(steer, f"(turn {key})"):
        return
    durable_append(steer, f"\n## Update {time.strftime('%Y-%m-%d %H:%M')}{f' (turn {key})' if key else ''}\n{text.strip()}\n")


def _needless_ask(a: dict, text: str) -> str:
    """Why an ask is the coordinator's own call, or "". Review and merge asks always go out."""
    if a.get("blocking") in ("review", "merge"):
        return ""
    rec = str(a.get("recommendation") or "")
    if not _YES_RE.match(rec):
        return ""
    if a.get("blocking") == "restriction" and _RETIRE_RE.search(f"{text} {rec}"):
        return ("a restriction that is clearly over is retired, not asked about: charter_update with `quote` (the "
                "item) or `replaces` (its section), `text` (what still holds) and `over` (the end that passed); the "
                "user is told. "
                "Ask only when it is truly unclear whether it is over, and then do not recommend yes")
    if a.get("blocking") in ("restriction", "irreversible") and _calls_undoable(f"{text} {rec}"):
        return ("you recommend yes to a step you call reversible: a known, safe, reversible fix is yours. Do it "
                "(task_add or the action), memory_add the decision and notify at severity low")
    return ""


_CLASS_FOR = {"irreversible": "irreversible", "restriction": "restriction_change"}


def _unclassified_ask(a: dict, least: str) -> tuple[str, str] | None:
    """(refusal label, why) for an irreversible or restriction ask whose `classify` is missing, is
    `neither`, or does not agree with `blocking`, or an irreversible ask that does not name the
    reversible alternative it considered; else None. Access, funds, spend, review, merge and human
    asks carry a reason only the user or another person can clear, so they need no class."""
    blocking = a.get("blocking")
    want = _CLASS_FOR.get(blocking)
    if not want:
        return None
    cls = a.get("classify")
    if cls not in ASK_CLASSES:
        return ("unclassified", f"classify it first: set `classify` to `{want}` for blocking `{blocking}`. "
                "`irreversible`: the step cannot be undone; `restriction_change`: it breaks or changes a "
                "restriction; `neither`: decide it yourself")
    if cls == "neither":
        return ("neither", "decide it yourself. A step that is neither irreversible nor a restriction change is a "
                "judgment call: act on it, memory_add a decision and notify at severity low")
    if cls != want:
        return ("classify mismatch", f"`classify` {cls} does not match blocking `{blocking}`: set `classify` to "
                f"`{want}`, or change `blocking` to the reason that is true")
    if blocking == "irreversible" and len(least) < LEAST_DISRUPTIVE_MIN:
        return ("no reversible alternative", "name the reversible alternative you considered in least_disruptive, "
                "and why it does not do. If one does, take it (task_add) and memory_add the decision instead of "
                "asking")
    return None


# The self-health gate is a high-precision backstop; model-free heal checks (heal.py) do the
# self-healing. It refuses an ask only when, in one sentence, an anomaly word (or a restart-type
# verb) sits within _NEAR words of something the project runs, or when an auto power cycle is off.
# Known gaps, accepted: paraphrases without these words ("the box no longer answers", "a hold never
# cleared", "the queue is wedged") get through, and heal checks cover them.
_NEAR = 4
_ANOMALY = {"dead", "died", "dies", "hung", "stuck", "crashed", "failed", "failing", "stopped", "exited", "disabled"}
_ANOMALY_PAIRS = {("is", "off"), ("turned", "off"), ("are", "off"), ("was", "off")}
_DOWN_AFTER = {"is", "are", "was", "were", "went", "been", "still", "stays", "gone"}   # "is down", not "scale down"
_RESTART = {"re-enable", "reenable", "restart", "rerun", "re-run", "retry", "relaunch"}
_OBJECTS = {"worker", "task", "job", "watcher", "schedule", "daemon", "runner", "broker", "timer", "service"}
_RUN_BEFORE = {"a", "an", "the", "this", "that", "its", "their", "our", "my", "last", "nightly", "daily", "latest"}
_AUTO_POWER_RE = re.compile(r"\b(?:auto(?:matic)?[- ]?power[- ]?cycl\w*|power[- ]cycling)\b", re.I)
_POWER_OFF_RE = re.compile(r"\b(?:is off|are off|turned off|switched off|disabled|re-?enabl\w*|turn (?:it )?back on)\b",
                           re.I)
# Exemptions, checked first. A real credential (bare "key" or "login" is not one):
_CREDENTIAL_RE = re.compile(r"\b(?:ssh keys?|api keys?|tokens?|logged out|log in again|needs? an? login|passwords?)\b",
                            re.I)
# Under access or human only: a step only the user can take, or another person's request.
_USER_ONLY_RE = re.compile(r"\b(?:physical(?:ly)?|on[- ]site|by hand|power button|your (?:laptop|machine)|"
                           r"the user's (?:laptop|machine)|reverse tunnel)\b", re.I)
_OTHERS_ASK_RE = re.compile(r"\b(?:another|other|a|an|the|their)\s+(?:team|person|people|reviewer|reporter|"
                            r"maintainer)s?\b(?:\s+\w+){0,2}?\s+(?:asked|requested)\b", re.I)
# The restriction or the irreversible step a self-health ask names in least_disruptive.
_NAMES_LIMIT_RE = re.compile(
    r"\b(restrict\w*|rules?|charter|forbid\w*|bans?|banned|not allowed|never|breaks?|irreversibl\w*|"
    r"undone|permanent\w*|cannot undo|can't undo)\b", re.I)


def _health_hit(text: str) -> bool:
    """True when one sentence has an anomaly or restart word within _NEAR words of an object the
    project runs, or says an auto power cycle is off."""
    for sentence in re.split(r"[.;:!?\n]+", text.replace("’", "'")):
        if _AUTO_POWER_RE.search(sentence) and _POWER_OFF_RE.search(sentence):
            return True
        words = [re.sub(r"'s$", "", w.strip("\"'*_(),")).lower() for w in sentence.split()]
        hits, objs = [], []
        for i, w in enumerate(words):
            prev = words[i - 1] if i else ""
            if (w in _ANOMALY or w in _RESTART or (w == "down" and prev in _DOWN_AFTER)
                    or (prev, w) in _ANOMALY_PAIRS):
                hits.append(i)
            stem = w[:-1] if w.endswith("s") else w
            if stem in _OBJECTS or (stem == "run" and prev in _RUN_BEFORE):
                objs.append(i)
        if any(abs(h - o) <= _NEAR for h in hits for o in objs):
            return True
    return False


def _self_health_ask(a: dict, text: str, least: str = "") -> str:
    """Why an ask about the project's own health (a dead worker, a disabled schedule, an auto power
    cycle that is off) is the project's to fix, or "". Exempt: a real credential; under access or
    human, a step only the user can take or another person's request; under irreversible or
    restriction, least_disruptive naming the restriction or the irreversible step. Review, merge,
    funds and spend asks pass."""
    blocking = a.get("blocking")
    if blocking not in ("human", "access", "irreversible", "restriction"):
        return ""
    said = f"{text} {a.get('recommendation') or ''}"
    if _CREDENTIAL_RE.search(said):
        return ""
    if blocking in ("human", "access") and (_USER_ONLY_RE.search(said) or _OTHERS_ASK_RE.search(said)):
        return ""
    if not _health_hit(said):
        return ""
    if blocking in ("irreversible", "restriction") and _NAMES_LIMIT_RE.search(least):
        return ""
    return ("keeping the project running is yours: fix what it runs yourself and report afterwards. Queue the fix "
            "(task_add priority 1) or a heal check (schedule_set), memory_add the decision and notify at severity "
            "low. If the fix lives in another project's harness, a task sends it there with `ttp note --to "
            "<project>`. Ask only for a real missing credential, a step only the user can take (physical, on their "
            "machine) or another person's request, or when every fix breaks a restriction: blocking restriction "
            "(or irreversible), with least_disruptive naming the restriction or the irreversible step")


def _count_refusal(db, key: str | None, label: str, text: str) -> None:
    """Record a refused ask for the unblocking metrics, once per turn action (a replayed turn adds none)."""
    fp = f"{ASK_REFUSED_KIND}:{key}" if key else None
    if fp and db.one("SELECT 1 FROM events WHERE kind=? AND fingerprint=?", (ASK_REFUSED_KIND, fp)):
        return
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
         (time.time(), "coordinator", ASK_REFUSED_KIND, fp, "low", f"{label}: {clip(text, 300)}", "handled"))


def _calls_undoable(text: str) -> bool:
    """True only when the text plainly calls the step reversible: some "reversible" / "can be undone"
    is not hedged by a negation or qualifier in the few words before it ("isn't", "not easily",
    "only partly"), and nothing calls it irreversible. When unsure the ask goes out: a wrongly
    rejected ask would push the coordinator to take an irreversible step itself."""
    text = text.replace("\u2019", "'")
    if _NOT_UNDOABLE_RE.search(text):
        return False
    for m in _UNDOABLE_RE.finditer(text):
        clause = re.split(r"[.;:!?,()\n]", text[:m.start()])[-1]
        words = [w.lower().strip("\"'*_") for w in clause.split()[-5:]]
        if not any(w in _UNDO_HEDGES or w.endswith("n't") or w.startswith("non") for w in words):
            return True
    return False


def _ws(text: str) -> str:
    return " ".join(text.split())


def _users_own(db, messages: list[int]) -> list[int]:
    """The ids among `messages` of the user's own messages: none the harness posted (provenance system)."""
    if not messages:
        return []
    rows = db.q(f"SELECT id, provenance FROM messages WHERE id IN ({','.join('?' * len(messages))}) "
                f"AND direction='in' AND kind='user'", list(messages))
    return [r["id"] for r in rows if (r["provenance"] or "") != "system"]


def _record_charter_approval(p: Project, messages: list[int], section: str, quote: str, replaces: str,
                             text: str, end: dict | None, failed: str = "") -> None:
    """Record a charter change the user's turn asked for that failed to apply (an ambiguous heading,
    a quote that matched twice), so a retry in a later turn without a user message is still the
    user's word. Only from a user's own message of this turn: none from the harness itself
    (provenance system). A change with an end is not carried (its end was relative to that turn)."""
    if end or not messages:
        return
    db = p.db
    said = _users_own(db, messages)
    if not said:
        return
    from .prompts import charter_sections
    try:
        sections = charter_sections(p.charter_path.read_text())
    except FileNotFoundError:
        sections = []
    cands = [" ".join(sections[i][0][3:].split()) for i in _replaces_hits(sections, replaces)] if replaces else []
    ask = db.one("SELECT id FROM messages WHERE kind='ask' AND direction='out' AND id<? ORDER BY id DESC LIMIT 1",
                 (min(said),))
    sha = hashlib.sha256(_ws(text).encode()).hexdigest()
    rec = {"section": _ws(section).lower(), "quote": _ws(quote), "replaces": _ws(replaces.lstrip("#")),
           "text": text, "sha": sha}
    with db.tx():
        have = db.kv(CHARTER_APPROVALS_KEY, []) or []
        if any(all(x.get(k) == v for k, v in rec.items()) and not x.get("used") for x in have):
            return   # a retried turn records it once
        rec.update(id=hashlib.sha256(f"{said}{sha}{time.time()}".encode()).hexdigest()[:12], messages=said,
                   ask=ask["id"] if ask else None, candidates=cands, failed=failed[:300], ts=time.time(), used=None,
                   guard=CHARTER_GUARD_VERSION)
        db.set_kv(CHARTER_APPROVALS_KEY, (have + [rec])[-20:])


def _charter_approval(p: Project, section: str, quote: str, replaces: str, text: str) -> tuple[dict | None, str]:
    """The recorded, unused, unexpired approval (see _record_charter_approval) this change matches
    exactly: same section, same `quote` or the same section `replaces` names (by any of the names
    that resolve to it, or one of the headings an ambiguous one matched), the same text up to
    whitespace. Else (None, why none counts)."""
    have = p.db.kv(CHARTER_APPROVALS_KEY, []) or []
    if not have:
        return None, "No approval of the user's is on record for this change"
    try:
        days = float(p.config()["coordinator"].get("charter_approval_days", CHARTER_APPROVAL_DAYS))
    except (KeyError, TypeError, ValueError):
        days = CHARTER_APPROVAL_DAYS
    from .prompts import charter_sections
    sections = charter_sections(p.charter_path.read_text())
    hits = _replaces_hits(sections, replaces) if replaces else []
    now_name = " ".join(sections[hits[0]][0][3:].split()) if len(hits) == 1 else None
    why, found = "No approval of the user's is on record for this change (same section and quote or replaces)", ""
    sha = hashlib.sha256(_ws(text).encode()).hexdigest()
    for rec in reversed(have):   # newest first; a refusal says why the newest one for this target fails
        # Adding the very text the user approved removes nothing: it matches in any section (a change
        # the guard refused is also recorded under each item it named, see _reject_contradicting_append).
        appends = text and not quote and not replaces and rec["sha"] == sha
        if not appends and (rec["section"] != _ws(section).lower() or rec["quote"] != _ws(quote)):
            continue
        if not appends and (rec["replaces"] or replaces) and not (
                rec["replaces"].lower() == _ws(replaces.lstrip("#")).lower()
                or now_name is not None and now_name in rec["candidates"]):
            continue
        ids = ", #".join(map(str, rec["messages"]))
        if rec["sha"] != sha:
            found = found or (f"The user's yes in message #{ids} was for other text: "
                              f"{clip(_ws(rec['text']), 300)!r}; send that text exactly, or ask again for this one")
        elif rec.get("used"):
            found = found or f"The user's yes in message #{ids} was already used once"
        elif time.time() - rec["ts"] > days * 86400:
            found = found or f"The user's yes in message #{ids} expired after {days:g} days; ask again"
        else:
            return rec, ""
    return None, found or why


def _use_charter_approval(p: Project, rid: str, key: str | None) -> None:
    """Mark an approval used, with the other records of the same change (the same text from the same
    messages: as sent and as the quoted resend of each item the guard named): one yes, one use."""
    with p.db.tx():
        have = p.db.kv(CHARTER_APPROVALS_KEY, []) or []
        mine = next((r for r in have if r.get("id") == rid), None)
        for rec in have:
            if mine and rec.get("sha") == mine.get("sha") and rec.get("messages") == mine.get("messages") \
                    and not rec.get("used"):
                rec["used"] = {"ts": time.time(), "turn": key}
        p.db.set_kv(CHARTER_APPROVALS_KEY, have)


# Raise this when the charter_update guard changes what it lets through, and pin the new source
# with it: a test fails when the source of CHARTER_GUARD_FUNCS no longer has CHARTER_GUARD_PIN's
# sha256. A user's change that failed under an older guard for another reason than the guard
# (reraise_charter_changes) is then raised at once.
CHARTER_GUARD_VERSION = 3
CHARTER_GUARD_FUNCS = ("_reject_contradicting_append", "_append_conflicts", "_overlaps", "_quote_hits",
                       "_item_quote_hits", "_items", "_norm", "_may_lift")
CHARTER_GUARD_PIN = (3, "1dd6797f084673b4177959b53a76aced9468cef9c17dcc50cd79b8346cc2edd9")
CHARTER_RERAISE_KEY = "charter_reraise"   # kv: {"version", "ts"} of the last re-raise pass


def reraise_charter_changes(p: Project, now: float | None = None) -> list[str]:
    """Raise a charter change of the user's that failed for a reason other than the guard (an
    ambiguous heading, a quote that named no one item) and is still pending (recorded by
    _record_charter_approval, unused, unexpired, not in the charter yet) to the coordinator again,
    as a queued charter_retry event: once per change (marked `raised` on its newest record), a
    day after it failed, or on the first pass after a guard change (CHARTER_GUARD_VERSION) when it
    failed under an older guard. A change whose newest failure is the guard's refusal is never
    raised again: the coordinator had the refusal in its next digest, with its two fixes (the
    quoted resend of the item, or `both_hold`), and the user's word stays on record for either.
    One pass per guard change and per day. No model. Returns the new events' texts."""
    now = time.time() if now is None else now
    db = p.db
    state = db.kv(CHARTER_RERAISE_KEY) or {}
    if state.get("version") == CHARTER_GUARD_VERSION and now - float(state.get("ts") or 0) < 86400:
        return []
    db.set_kv(CHARTER_RERAISE_KEY, {"version": CHARTER_GUARD_VERSION, "ts": now})
    try:
        days = float(p.config()["coordinator"].get("charter_approval_days", CHARTER_APPROVAL_DAYS))
    except (KeyError, TypeError, ValueError):
        days = CHARTER_APPROVAL_DAYS
    try:
        charter = _ws(p.charter_path.read_text())
    except OSError:
        return []
    out = []
    with db.tx():
        have = db.kv(CHARTER_APPROVALS_KEY, []) or []
        newest: dict[str, dict] = {}
        for rec in have:   # oldest first: the newest record of each change wins
            if not rec.get("used") and rec.get("failed"):
                newest[rec["sha"]] = rec
        for rec in newest.values():
            age = now - float(rec.get("ts") or 0)
            older_guard = int(rec.get("guard") or 0) < CHARTER_GUARD_VERSION
            if (rec.get("raised") or GUARD_REJECTED in rec["failed"] or age > days * 86400
                    or age < 86400 and not older_guard):
                continue
            rec["raised"] = now
            text = _ws(rec["text"])
            if text and text in charter or not text and rec["quote"] and rec["quote"] not in charter:
                continue   # it landed meanwhile
            sent = (f"section {rec['section']!r}" + (f", `quote` {rec['quote']!r}" if rec["quote"] else "")
                    + (f", `replaces` {rec['replaces']!r}" if rec["replaces"] else "")
                    + f", `text` {rec['text'].strip()!r}")
            msg = (f"Pending charter change from the user's message #{', #'.join(map(str, rec['messages']))}, "
                   f"refused on {datetime.fromtimestamp(float(rec['ts'])).strftime('%Y-%m-%d')} "
                   f"({clip(rec['failed'], 300)}). It was sent as: {sent}. "
                   + ("The charter guard changed since, so it may go through as sent; else fix"
                      if older_guard else "Fix")
                   + " what that error names and send it again (no ask: the user's yes on record covers the "
                     "same section, target and text, and for a `replaces` any heading it matched). Raised once "
                     "only. If the user has since said otherwise, leave it.")
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (now, "harness", "charter_retry", "normal", msg, "queued"))
            out.append(msg)
        db.set_kv(CHARTER_APPROVALS_KEY, have)
    return out


_DATED = re.compile(r"\s*\(added [^)]*\)$", re.I)   # legacy "## Policies (added 2026-09-30, turn 4.0)"


def _charter_update(p: Project, section: str, text: str, quote: str, replaces: str, key: str | None,
                    user_turn: bool, over: str = "", end: dict | None = None,
                    no_approval: str = "", source: list[int] | None = None) -> tuple[str, str, str]:
    """Apply one charter_update: retire the section `replaces` names, then edit the one section
    `section` names. With `quote`, the single span of that section matching it is replaced by
    `text` (or removed when `text` is empty); otherwise `text` is added at the section's end, and
    the section is created when there is none. Text with an `end` (see ends.from_action) gets a
    dated section of its own instead, so the daemon retires only it. Whatever is removed or
    replaced, and every addition, goes to CHARTER_HISTORY, which also marks the turn done for a
    retried turn. Outside a user turn a restriction is removed or replaced only with `over`: the
    end condition that has clearly passed (`no_approval` says why no recorded approval of the
    user's covered it). A restriction the user's word changes or lifts (`quote` or `replaces`)
    also merges every permanent Restrictions section into one block (_merge_restrictions).
    Returns the edited section's name, a note for the commit message and, when `over` retired a
    restriction, what it retired (else ""). `source`: the user's messages the change came from,
    named in CHARTER_HISTORY."""
    from .prompts import charter_sections
    sections = charter_sections(p.charter_path.read_text())
    hist = p.harness / CHARTER_HISTORY
    stamp = time.strftime("%Y-%m-%d") + (f", turn {key}" if key else "")
    said = f", user message #{', #'.join(map(str, source))}" if source else ""
    logged = time.strftime("%Y-%m-%d") + said + (f", turn {key}" if key else "")   # ends ", turn <key>)" too
    log: list[str] = []
    # Logged by an earlier try of this turn: write the charter only if that try died before it did.
    retried = bool(key) and _has_line(hist, f", turn {key})")
    flat = " ".join(" ".join(line for h, b in sections for line in [h, *b]).split())
    if retried and (" ".join(text.split()) in flat if text else not _charter_quoted(sections, section, quote)
                    if quote else _charter_replaced(sections, replaces, quiet=True) is None):
        return section, "", ""
    extra, retired = "", ""
    by_word = user_turn or len(over) >= OVER_MIN
    why = ("needs the user's word, or `over`: the end condition that has clearly passed (what ended it and when). "
           "If it is truly unclear whether it is over, ask_user (blocking restriction)"
           + (f". {no_approval}" if no_approval else ""))
    lifted = False   # the user's word changed or lifted a restriction
    if replaces:
        i = _charter_replaced(sections, replaces)
        old_head, old_body = sections[i]
        name = " ".join(old_head[3:].split())
        if name.lower().startswith("brief"):
            raise ValueError("charter_update: the Brief is the user's own words and is never replaced")
        if name.lower().startswith("restriction") and not by_word:
            raise ValueError(f"charter_update: replacing the restriction section {name!r} {why}")
        if name.lower().startswith("restriction") and not user_turn:
            retired = f"section \"{name}\""
        lifted = user_turn and name.lower().startswith("restriction")
        del sections[i]
        log.append(f"{old_head}\n(replaced by an update to {section}, {logged}" + (f"; over: {over}" if over else "")
                   + ")\n" + "\n".join(old_body).strip("\n"))
        extra = f" (replaces {name})"
    names = [" ".join(h[3:].split()) for h, _ in sections]
    t = _charter_target(names, section, [ends.charter_end(b) for _, b in sections], bool(quote))
    if (text or quote) and t is not None and names[t].lower().startswith("brief"):
        raise ValueError("charter_update: the Brief is the user's own words; put the update in another section")
    if quote:
        if t is None:
            raise ValueError(f"charter_update: no charter section {section!r} to remove or replace an item in; "
                             f"sections: " + "; ".join(n for n in names if n))
        t, hits = _charter_quote_spot(sections, t, section, quote)
        if names[t].lower().startswith("restriction") and not by_word:
            raise ValueError(f"charter_update: removing or replacing a restriction {why}")
        body = "\n".join(sections[t][1])
        if len(hits) != 1:
            near = _closest_items(sections, quote) if not hits else []
            raise ValueError(f"charter_update: `quote` matches {len(hits)} times in {names[t]!r}; quote the exact "
                             f"text of one item, long enough to be unique there"
                             + ("; closest: " + "; ".join(f"\"{clip(i, 800)}\" (section {n!r})" for i, n in near)
                                + ": resend one of them exactly as `quote`, with its section" if near else ""))
        s, e = hits[0]
        if names[t].lower().startswith("restriction") and not user_turn:
            retired = f"\"{clip(' '.join(body[s:e].split()), 200)}\""
        lifted = user_turn and names[t].lower().startswith("restriction")
        log.append(f"### {'Replaced in' if text else 'Removed from'} {names[t]} ({logged})\n{body[s:e]}"
                   + (f"\nNow: {text}" if text else "") + (f"\nOver: {over}" if over and not user_turn else ""))
        # Text going into a bullet keeps that bullet's marker, not one of its own ("- - ...").
        new = re.sub(r"^\s*" + _BULLET, "", text, count=1) if text and _in_bullet(body, s) else text
        sections[t] = (sections[t][0], _cut(body, s, e, new).split("\n"))
    elif text and end:
        name = _DATED.sub("", section)
        name = name[:1].upper() + name[1:]
        heading, n = f"## {name} (added {stamp})", 2
        while heading[3:] in names:
            heading, n = f"## {name} (added {stamp}, {n})", n + 1   # unique, so `replaces` can name it
        sections.append((heading, (text + ends.charter_tail(end)).split("\n")))
        t, names = len(sections) - 1, names + [heading[3:]]
        log.append(f"### Added to {names[t]} ({logged})\n{text}{ends.charter_tail(end)}")
    elif text:
        if t is None:
            name = _DATED.sub("", section)
            name = name[:1].upper() + name[1:]
            sections.append((f"## {name}", []))
            t, names = len(sections) - 1, names + [name]
        body = _drop_placeholders(sections[t][1])
        while body and not body[-1].strip():
            body.pop()
        bullets = bool(body) and body[-1].lstrip().startswith("- ") and text.startswith("- ")
        sections[t] = (sections[t][0], body + ([] if bullets or not body else [""]) + text.split("\n"))
        log.append(f"### Added to {names[t]} ({logged})\n{text}")
    if lifted:
        target = sections[t][0] if t is not None else None
        sections = _merge_restrictions(sections, log, stamp)
        names = [" ".join(h[3:].split()) for h, _ in sections]
        t = next((i for i, (h, _) in enumerate(sections) if h == target), None)
        if t is None and target:   # merged into the one Restrictions block
            t = next((i for i, n in enumerate(names) if n.lower().startswith("restriction")
                      and not _DATED.search(n)), None)
    if not hist.exists():
        durable_append(hist, "# Charter history\n\nWhat was added to CHARTER.md, and what was removed or replaced "
                             "there, oldest first.\n")
    if not retried:
        durable_append(hist, "".join(f"\n{entry}\n" for entry in log))
    out = []
    for k, (h, b) in enumerate(sections):
        b = list(b)
        while b and not b[-1].strip():
            b.pop()
        out += ([h] if h else []) + b + ([""] if k < len(sections) - 1 else [])
    durable_write(p.charter_path, "\n".join(out).rstrip() + "\n")
    return (names[t] if t is not None else section), extra, retired


def _merge_restrictions(sections: list[tuple[str, list[str]]], log: list[str],
                        stamp: str) -> list[tuple[str, list[str]]]:
    """Merge every permanent Restrictions section (the base one and dated "(added ...)" ones) into
    one block under the base heading, so workers get a single binding list. Each item is kept
    once; a prose paragraph becomes one bullet. Temporary sections (with an `Expires:`, `Until:`
    or `Until probe:` end) stay separate: the daemon retires a section as a whole when its end
    passes (ends.py), so their items keep their end only in a section of their own. The merge is
    logged in `log` (for CHARTER_HISTORY), with the moved text."""
    names = [" ".join(h[3:].split()) for h, _ in sections]
    idx = [i for i, (h, b) in enumerate(sections)
           if names[i].lower().startswith("restriction") and not ends.charter_end(b)]
    if len(idx) < 2:
        return sections
    base = next((i for i in idx if not _DATED.search(names[i])), idx[0])
    head = sections[base][0]
    if _DATED.search(names[base]):
        head = "## " + _DATED.sub("", names[base])
    body = _drop_placeholders(list(sections[base][1]))
    while body and not body[-1].strip():
        body.pop()
    have = {" ".join(x.strip().lstrip("-*+ ").split()).lower() for x in body if x.strip()}
    moved = []
    for i in idx:
        if i == base:
            continue
        moved.append(f"{names[i]}:\n" + "\n".join(sections[i][1]).strip("\n"))
        for par in "\n".join(_drop_placeholders(list(sections[i][1]))).split("\n\n"):
            lines = [x for x in par.split("\n") if x.strip()]
            if not lines:
                continue
            if not any(re.match(r"\s*(?:[-*+]|\d+[.)])\s", x) for x in lines):
                lines = ["- " + " ".join(" ".join(lines).split())]
            new = [x for x in lines if " ".join(x.strip().lstrip("-*+ ").split()).lower() not in have]
            have |= {" ".join(x.strip().lstrip("-*+ ").split()).lower() for x in new}
            if new and body and not re.match(r"\s*(?:[-*+]|\d+[.)])\s", body[-1]):
                body.append("")
            body += new
    log.append(f"### Merged into {_DATED.sub('', names[base])} ({stamp})\n" + "\n\n".join(moved))
    return [(head, body) if i == base else sec for i, sec in enumerate(sections) if i == base or i not in idx]


def _replaces_hits(sections: list[tuple[str, list[str]]], replaces: str) -> list[int]:
    """The sections `replaces` names: its number, its full heading (as the charter or the digest's
    heading line shows it, with or without `## ` or quotes), else every heading it starts."""
    want = " ".join(replaces.strip().strip("\"'`").lstrip("#").split()).lower()
    names = [" ".join(h[3:].split()) for h, _ in sections]
    headed = [i for i, n in enumerate(names) if n]
    if want.rstrip(".").isdigit():
        k = int(want.rstrip("."))
        return [headed[k - 1]] if 0 < k <= len(headed) else []
    return ([i for i in headed if names[i].lower() == want]
            or [i for i in headed if want and names[i].lower().startswith(want)])


def _charter_replaced(sections: list[tuple[str, list[str]]], replaces: str, quiet: bool = False) -> int | None:
    """The index of the one section `replaces` names (see _replaces_hits). None when it names no
    one section and `quiet`; otherwise that raises, listing the candidates."""
    hits = _replaces_hits(sections, replaces)
    if len(hits) != 1 and quiet:
        return None
    if len(hits) != 1:
        names = [" ".join(h[3:].split()) for h, _ in sections]
        number = {i: k for k, i in enumerate((i for i, n in enumerate(names) if n), 1)}
        listed = hits or list(number)
        raise ValueError(f"charter_update: `replaces` {replaces!r} matches {len(hits)} charter sections; give the "
                         f"full heading or the number of one of " + ("these" if hits else "the charter's")
                         + ": " + "; ".join(f"{number[i]}. {names[i]}" for i in listed))
    return hits[0]


def charter_headings(p: Project) -> str:
    """The charter's section headings, numbered as `replaces` takes them, on one line."""
    from .prompts import charter_sections
    try:
        sections = charter_sections(p.charter_path.read_text())
    except FileNotFoundError:
        return ""
    names = [n for n in (" ".join(h[3:].split()) for h, _ in sections) if n]
    return " · ".join(f"{k}. {n}" for k, n in enumerate(names, 1))


def _charter_target(names: list[str], section: str, ends_of: list[dict] | None = None,
                    quoting: bool = False) -> int | None:
    """The one section an update to `section` goes to: the heading itself; else the section whose
    heading is `section` plus a parenthesised note or more words ("Restrictions (binding ...)",
    "Goals and success criteria"), preferring one that is not a legacy "(added ...)" section.
    A temporary section (one whose body ends in an end, see ends.charter_end) takes no additions:
    its end would retire them with it. Only `quoting` edits one."""
    want = section.lower()
    if ends_of is not None and not quoting:
        names = [n if not ends_of[i] else "" for i, n in enumerate(names)]
    exact = [i for i, n in enumerate(names) if n and n.lower() == want]
    if exact:
        return exact[0]
    base = _DATED.sub("", want)
    hits = [i for i, n in enumerate(names) if n and (_base_heading(n) == base
                                                    or n.lower().startswith(base + " "))]
    undated = [i for i in hits if not _DATED.search(names[i])]
    return (undated or hits or [None])[0]


def _charter_quoted(sections: list[tuple[str, list[str]]], section: str, quote: str) -> bool:
    """Whether `quote` still matches exactly once in the section an update to `section` edits."""
    t = _charter_target([" ".join(h[3:].split()) for h, _ in sections], section)
    if not quote or t is None:
        return False
    try:
        return len(_charter_quote_spot(sections, t, section, quote)[1]) == 1
    except ValueError:   # in several legacy sections: still there
        return True


def _base_heading(name: str) -> str:
    """A heading without its trailing parenthesised note, lower case: "Restrictions (added ...)" -> "restrictions"."""
    return re.sub(r"\s*\([^)]*\)$", "", name).lower()


def _charter_quote_spot(sections: list[tuple[str, list[str]]], t: int, section: str,
                        quote: str) -> tuple[int, list[tuple[int, int]]]:
    """The section a `quote` edit goes to, and where `quote` matches in it: section `t`, unless it
    matches nowhere there; then the one other section with the same base heading (a legacy
    "(added ...)" duplicate) holding its only match. Raises when it matches in several of those."""
    hits = _quote_hits("\n".join(sections[t][1]), quote)
    if hits:
        return t, hits
    names = [" ".join(h[3:].split()) for h, _ in sections]
    bases = {_base_heading(names[t]), _base_heading(section)}
    found = [(i, h) for i, n in enumerate(names) if i != t and n and _base_heading(n) in bases
             for h in [_quote_hits("\n".join(sections[i][1]), quote)] if h]
    if len(found) == 1 and len(found[0][1]) == 1:
        return found[0]
    if found:
        raise ValueError(f"charter_update: `quote` matches 0 times in {names[t]!r} but in "
                         + ", ".join(f"{names[i]!r} ({len(h)} times)" for i, h in found)
                         + "; give that section's full heading as `section`, and quote one item long enough "
                           "to be unique there")
    return t, hits


def _quote_hits(body: str, quote: str) -> list[tuple[int, int]]:
    """Where `quote` occurs in `body`, as (start, end) spans, line breaks and runs of spaces counting
    as one space. A quote that starts or ends with a letter or digit matches only whole words there
    ("ever merge." does not match inside "Never merge."). A quote found nowhere that way may still
    name one item (_item_quote_hits)."""
    words = quote.split()
    if not words:
        return []
    pat = r"\s+".join(map(re.escape, words))
    pat = (r"(?<!\w)" if re.match(r"\w", words[0]) else "") + pat + (r"(?!\w)" if re.search(r"\w$", words[-1]) else "")
    return [m.span() for m in re.finditer(pat, body)] or _item_quote_hits(body, quote)


_EMPH = "*_`"   # markdown emphasis and code marks
_SMART = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
_BULLET = r"(?:[-*+]|\d+[.)])\s+"
_END_PUNCT = ".,;:!?"


def _item_quote_hits(body: str, quote: str) -> list[tuple[int, int]]:
    """Where a `quote` that matches nowhere exactly names one item of `body` (_items), compared
    ignoring a leading bullet marker, case, runs of spaces, smart quotes and end punctuation
    (_norm): the item it equals as a whole (the span is the item's text after its bullet marker),
    else the one place it occurs inside an item, whole words only (the span is that text, with the
    end punctuation that follows it when the quote ends with some). Each item is compared on its
    own, so a span never reaches across items. Anything else (no item, several, text spanning
    items) matches nothing, and the rejection names the closest items. Known gap, by design: a
    quote that spans several items needs their exact text, or `replaces`."""
    want = _norm(re.sub(r"^\s*" + _BULLET, "", quote))[0]
    if not want:
        return []
    items = [(s, e, *_norm(body[s:e])) for s, e, _ in _items(body)]
    whole = [(s, e) for s, e, have, _ in items if have == want]
    if whole:
        return whole if len(whole) == 1 else []
    pat = ((r"(?<!\w)" if re.match(r"\w", want) else "") + re.escape(want)
           + (r"(?!\w)" if re.search(r"\w$", want) else ""))
    spans = [(s + at[m.start(1)], s + at[m.end(1) - 1] + 1, e)
             for s, e, have, at in items for m in re.finditer(f"(?=({pat}))", have)]
    if len(spans) != 1:
        return []
    s, end, e = spans[0]
    if quote.rstrip()[-1:] in _END_PUNCT:   # the quote's end punctuation names the item's
        while end < e and body[end] in _END_PUNCT:
            end += 1
    return [(s, end)]


def _items(body: str) -> list[tuple[int, int, bool]]:
    """The items of a section's body: each bullet with the lines that continue it, and each
    paragraph, as (start of its text after any bullet marker, end of its text, whether it is a
    bullet). A blank line or a bullet ends an item."""
    out: list[list] = []
    cur, pos = None, 0
    for line in body.split("\n"):
        end = pos + len(line.rstrip())
        bullet = re.match(r"\s*" + _BULLET, line)
        if not line.strip():
            cur = None
        elif bullet or cur is None:
            cur = [pos + (bullet.end() if bullet else len(line) - len(line.lstrip())), end, bool(bullet)]
            out.append(cur)
        else:
            cur[1] = end
        pos += len(line) + 1
    return [(s, e, b) for s, e, b in out]


def _norm(text: str) -> tuple[str, list[int]]:
    """`text` as _item_quote_hits compares it: straight quotes, single spaces, case folded, no
    leading spaces or trailing spaces and end punctuation; with the offset in `text` of each of
    its characters."""
    out: list[str] = []
    at: list[int] = []
    for i, c in enumerate(text.translate(_SMART)):
        if c.isspace():
            if not out or out[-1] == " ":
                continue
            c = " "
        for x in c.casefold():
            out.append(x)
            at.append(i)
    while out and (out[-1] == " " or out[-1] in _END_PUNCT):
        out.pop()
        at.pop()
    return "".join(out), at


def _in_bullet(body: str, s: int) -> bool:
    """Whether offset `s` of a section's body is in a bullet's text, after its marker (_items)."""
    return any(b and a <= s <= e for a, e, b in _items(body))


def _loose(text: str) -> str:
    """Text compared loosely: straight quotes, no emphasis marks, bullets or trailing punctuation,
    single spaces, lower case."""
    t = text.translate(_SMART)
    t = re.sub(r"(?m)^\s*" + _BULLET, "", t)
    t = t.translate(str.maketrans("", "", _EMPH))
    return " ".join(t.split()).rstrip(".,;:!? ").lower()


def _closest_items(sections: list[tuple[str, list[str]]], quote: str, n: int = 2) -> list[tuple[str, str]]:
    """The charter items (bullets, paragraphs and their sentences; not the Brief) most like `quote`,
    as (item, section), best first, at most `n`, none below a 0.4 similarity."""
    from difflib import SequenceMatcher
    want, scored = _loose(quote), {}
    for heading, body in sections:
        name = " ".join(heading[3:].split())
        if not name or name.lower().startswith("brief"):
            continue
        cur: list[str] = []
        for line in body + [""]:
            if (re.match(r"\s*" + _BULLET, line) or not line.strip()) and cur:
                par = " ".join(" ".join(cur).split())
                for item in dict.fromkeys([re.sub("^" + _BULLET, "", par), *_sentences([par])]):
                    r = SequenceMatcher(None, want, _loose(item)).ratio()
                    if r >= 0.4 and r > scored.get((item, name), 0):
                        scored[(item, name)] = r
                cur = []
            if line.strip():
                cur.append(line.strip())
    return sorted(scored, key=lambda k: -scored[k])[:n]


def _cut(body: str, s: int, e: int, text: str) -> str:
    """`body` with body[s:e] replaced by `text`. A removal that empties its lines (a bullet
    marker aside) takes the lines with it; one inside a line keeps a single space."""
    if text:
        return body[:s] + text + body[e:]
    ls, le = body.rfind("\n", 0, s) + 1, body.find("\n", e)
    le = len(body) if le < 0 else le
    head, tail = body[ls:s], body[e:le]
    if re.fullmatch(r"\s*(?:[-*+]|\d+[.)])?\s*", head) and not tail.strip():
        out = body[:ls] + body[le + 1:]
    else:
        mid = head + tail.lstrip() if not head.strip() else head.rstrip() + (" " if tail.strip() else "") + tail.lstrip()
        out = body[:ls] + mid + body[le:]
    return re.sub(r"\n\s*\n(\s*\n)+", "\n\n", out)


def _drop_placeholders(lines: list[str]) -> list[str]:
    """`lines` without the whole paragraphs the template left to be filled in ("(none stated yet)"):
    a real entry replaces them."""
    from .prompts import _placeholder
    out, par = [], []
    for line in [*lines, ""]:
        if line.strip():
            par.append(line)
            continue
        s = " ".join(" ".join(par).split())
        if not (s.startswith("(") and _placeholder(s)):
            out += par
        out.append(line)
        par = []
    return out[:-1]


def _has_line(path: Path, ending: str) -> bool:
    try:
        return any(line.endswith(ending) for line in path.read_text().splitlines())
    except FileNotFoundError:
        return False


def open_task_count(p: Project) -> int:
    return len(p.db.q("SELECT id FROM tasks WHERE status NOT IN (%s)" % ",".join("?" * len(TERMINAL_TASK_STATES)),
                      TERMINAL_TASK_STATES))
