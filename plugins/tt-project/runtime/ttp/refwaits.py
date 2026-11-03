# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Queued waits that name an ask or a task, kept current, model-free (Daemon.refresh_ref_waits).

A queued task's wait note (its hand-off's `waiting_for`, its `blocked_reason`, a start_when's `start_why`)
may name what it waits on: `ask #12`, `task #34` or `#34`. Once that ask is resolved or that task is
done, the note is stale. The daemon then rewrites the note to say so, drops the cached probe verdicts
and reruns the task's probe at once (its retry_when or start_when). Its retry_when, dependencies,
start_when, start_after, not_before and attempts stay as they were. A done task is only a reason to check
again, not proof that its work landed: the probe or the next run decides. Each piece of evidence is acted
on once per wait (the hand-off's waiting_since, or the deferral's since).

Known gap, on purpose: a wait with no probe is never woken early. Its note is refreshed and it waits for
its normal retry, since evidence from before the wait began would otherwise wake it after every hand-off.
A bare `#N` is no task when a word before it names something else (`PR #8`, `PR: #8`, `the PR (#8)`,
`pull request #8`, `issue #8`), when it follows an owner/repo (`org/repo#8`, `org/repo #8`), or when it
continues a list such a word starts (`PRs #8 and #16`). Any other bare `#N` that is really a PR or issue
may still be read as a task; that only refreshes text or reruns a model-free probe.
"""
from __future__ import annotations

import re

SEEN_KEY = "ref_waits_seen"   # kv {task id: {"ep": wait episode, "seen": ["ask:12:resolved", ...]}}
FACTS_KEY = "refs_over"       # hand-off key: the facts the note carries, kept when the daemon rewrites it
HELD_NOTES = ("held: ",)      # a hold the daemon manages (logged out, network): its note stays
ASK_RE = re.compile(r"\basks?\s*#?\s*([0-9]+)\b", re.I)
TASK_RE = re.compile(r"\btasks?\s*#?\s*([0-9]+)\b", re.I)
# A bare `#N` counts as a task unless a word before it says it is something else (a PR, an issue, an ask).
BARE_RE = re.compile(r"(?<![\w#/])#([0-9]+)\b")
NOT_TASK_RE = re.compile(r"(?:\b(asks?|prs?|pull|pulls|requests?|issues?|bugs?|tickets?|runs?|steps?|items?|"
                         r"lines?|no\.?|number|change|turns?|commits?)|[\w.-]/[\w.-]+)[\s:(\[{'\"`*_‘“-]*$", re.I)
LIST_GAP_RE = re.compile(r"[\s,;&/)\]}'\"`*_’”]*(?:\b(?:and|or)\b)?[\s(\[{'\"`*_‘“]*$", re.I)


def refs(text: str, own: int | None = None) -> list[tuple[str, int]]:
    """The asks and tasks a wait note names, in order, without repeats (never the task itself)."""
    s = str(text or "")
    found: list[tuple[int, str, int]] = []
    for m in ASK_RE.finditer(s):
        found.append((m.start(), "ask", int(m.group(1))))
    for m in TASK_RE.finditer(s):
        found.append((m.start(), "task", int(m.group(1))))
    other = -1   # where the last bare `#N` that is no task ended
    for m in BARE_RE.finditer(s):
        before = s[max(0, m.start() - 24):m.start()]
        if NOT_TASK_RE.search(before) or (other >= 0 and LIST_GAP_RE.fullmatch(s, other, m.start())):
            other = m.end()
            continue
        found.append((m.start(), "task", int(m.group(1))))
    out: list[tuple[str, int]] = []
    for _, kind, n in sorted(found):
        if (kind, n) not in out and not (kind == "task" and n == own):
            out.append((kind, n))
    return out


def over(db, named: list[tuple[str, int]]) -> list[tuple[str, str]]:
    """(evidence key, plain fact) for each named ask that is resolved and each named task that is done."""
    out = []
    for kind, n in named:
        if kind == "ask":
            r = db.one("SELECT handled FROM messages WHERE id=? AND kind='ask'", (n,))
            if r and r["handled"]:
                out.append((f"ask:{n}:resolved", f"ask #{n} is resolved"))
        else:
            r = db.one("SELECT status FROM tasks WHERE id=?", (n,))
            if r and r["status"] == "done":
                out.append((f"task:{n}:done", f"task #{n} is done (not proof its work landed)"))
    return out


def with_facts(what: str, prev: dict) -> str:
    """A waiting hand-off's `waiting_for`, followed by what the daemon found about the asks and tasks it
    names, so a note rewritten later (its probe says not yet) does not bring back the stale reason."""
    facts = prev.get(FACTS_KEY) if isinstance(prev, dict) else None
    if not isinstance(facts, list) or not facts:
        return what
    return f"{what} ({'; '.join(str(f) for f in facts)})"
