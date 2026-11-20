"""A `blocked` hand-off whose only missing piece is another task's change landing is a model-free wait.

The rule is deliberately narrow. A blocked hand-off becomes a `landed:#<id>` wait only when its
question and summary together:
- name exactly one task (`#<id>`, or its `ttp/t<id>-` branch) that exists in this project and is not
  the task itself, with land/landed/pushed/merged wording;
- hold no '?' at all: any question may be a real one, so it reaches the coordinator;
- name no other `#<n>` style ref (`PR #n`, `PR: #n`, `pull request #n`, `owner/repo#n`, `owner/repo #n`, ...) and no other blocker (a credential,
  access, funds, a decision);
and the named task has not failed or been cancelled and is not already on the local push-branch ref.
Anything else stays `blocked`, unchanged.

Accepted gaps:
- A hand-off that waits only on a landing but is worded otherwise (asked as a question, naming two
  refs, no landing word) stays blocked and costs a coordinator turn.
- The landed check reads local refs only: result handling makes no fetch or other network call. A
  stale local ref can hide a landing until the next fetch: the task then waits, and its
  `landed:#<id>` probe (which fetches) passes on its first check."""
from __future__ import annotations

import re

REF_RE = re.compile(r"(?:#|\bttp/t)([0-9]+)\b")
LAND_RE = re.compile(r"\b(?:land(?:s|ed|ing)?|pushed|merged)\b", re.I)


OTHER_REF_RE = re.compile(r"(?:[\w/]|\b(?:prs?|pull requests?|issues?|ask|msg|message)[\s:(\[{'\"`*_‘“-]*"
                          r"|[\w.-]/[\w.-]+\s+)#[0-9]+\b", re.I)
OTHER_BLOCKER_RE = re.compile(
    r"credential|access|password|token|permission|funds|budget|approv|decid|decision|your call|should i|\bor\b",
    re.I)


def named_task(text: str) -> int | None:
    """The one task id the text names, or None for none, several, or any other `#<n>` style ref."""
    text = text or ""
    if OTHER_REF_RE.search(text):
        return None
    ids = {int(m) for m in REF_RE.findall(text)}
    return ids.pop() if len(ids) == 1 else None


def wait(p, task: dict, result: dict) -> dict | None:
    """The `waiting` hand-off for a blocked one waiting only on one task landing, else None."""
    if result.get("status") != "blocked":
        return None
    question = " ".join(str(result.get(k) or "") for k in ("question", "summary"))
    other = named_task(question)
    if other is None or other == task["id"] or not LAND_RE.search(question):
        return None
    if OTHER_BLOCKER_RE.search(question) or "?" in question:
        return None
    target = p.db.task(other)
    if not target or target["status"] in ("failed", "cancelled"):
        return None
    from ttp import landed
    if landed.check(p, p.root, tid=other, fetch=False)[0] != landed.NOT_YET:   # landed already, or unreadable
        return None
    asked = str(result.get("question") or result.get("summary") or "")[:500]
    return {**{k: v for k, v in result.items() if k != "question"}, "status": "waiting",
            "waiting_for": f"task #{other} landing", "retry_when": f"landed:#{other}",
            "retry_after_s": 1800, "wake_tier": "light", "wait_kind": "external",
            "summary": f"Waiting for #{other} to land (blocked question: {asked})"[:1500]}
