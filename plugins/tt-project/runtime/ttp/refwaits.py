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

A ref is another project's, never this one's, when a known peer's name stands right before it
(`<peer> task #8`, `<peer> #8`, `<peer>'s #8`, `<peer>#8`) or before the list it continues
(`<peer> #8, #9 and #12`). The peers (peer_names) are the registry's projects (`ttp list`), the projects
whose notes reached this one and the project's PEERS_FILE, read without a model call; this project's own
name and names that are plain words a note uses before a ref (`needs #12`) never count. Facts a note
kept from such a ref before (in refs_over and the note) are dropped (stale_facts).
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
# Words a note puts before a ref: a project with such a name never hides one (`needs #12` stays local).
PLAIN_WORDS = frozenset("""a an and or the this that its our own my your their for on to of in at by via per
from with after before until till see needs need waits wait waiting blocked landed done lands merge
merged fix fixes then also like re task tasks ask asks pr prs issue issues run runs""".split())
PEER_GAP = r"""(?:['’]s)?[\s:(\[{'"`*_‘“-]*"""
LIST_GAP_RE = re.compile(r"[\s,;&/)\]}'\"`*_’”]*(?:\b(?:and|or)\b)?[\s(\[{'\"`*_‘“]*$", re.I)


def peer_names(db, own_name: str | None = None, harness=None) -> set[str]:
    """Other projects' names a note may put before their own task ids (module doc), lowercased: the
    registry's projects, the senders of notes this project received and its PEERS_FILE, but its own name
    and plain words. Unreadable sources count as naming none."""
    from . import heldwait, project, upstream
    names: set[str] = set()
    try:
        names |= set((project.load_registry().get("projects") or {}))
    except Exception:
        pass
    try:
        names |= {str(r.get("project") or "") for r in (db.kv(upstream.KV_ROUTES) or {}).values()
                  if isinstance(r, dict)}
    except Exception:
        pass
    names |= heldwait.peers(harness)
    own = str(own_name or "").strip().lower()
    return {n for n in (str(x).strip().lower() for x in names)
            if n and n != own and n not in PLAIN_WORDS and project.NAME_RE.match(n)}


def _peer_re(peers) -> re.Pattern | None:
    """Matches the end of the text before a ref when a peer's name stands right before it."""
    names = sorted({str(n).lower() for n in peers or () if str(n).strip()}, key=len, reverse=True)
    if not names:
        return None
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?<![\w.-])(?:{alt}){PEER_GAP}(?:\b(?:tasks?|asks?)\b[\s:]*)?$", re.I)


def refs(text: str, own: int | None = None, peers=()) -> list[tuple[str, int]]:
    """The asks and tasks a wait note names, in order, without repeats (never the task itself, nor a task
    of another project: a name in `peers` stands right before it or before the list it continues)."""
    s = str(text or "")
    peer = _peer_re(peers)
    found: list[tuple[int, str, int]] = []
    for kind, rx in (("ask", ASK_RE), ("task", TASK_RE)):
        for m in rx.finditer(s):
            if not (peer and peer.search(s, max(0, m.start() - 80), m.start())):
                found.append((m.start(), kind, int(m.group(1))))
    other = -1   # where the last bare `#N` that is no task ended
    for m in BARE_RE.finditer(s):
        before = s[max(0, m.start() - 24):m.start()]
        if (NOT_TASK_RE.search(before) or (other >= 0 and LIST_GAP_RE.fullmatch(s, other, m.start()))
                or (peer and peer.search(s, max(0, m.start() - 90), m.start()))):
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


# The facts over() writes, as they stand in refs_over and in the note the daemon rewrote.
FACT_RE = re.compile(r"\b(ask|task) #([0-9]+) is (?:resolved|done \(not proof its work landed\))")


def stale_facts(facts, note: str, named: list[tuple[str, int]]) -> list[str]:
    """The facts kept in refs_over or written into the note `note` about an ask or task the wait no longer
    names as this project's own, such as one a peer's name stands before."""
    out: list[str] = []
    for m in [FACT_RE.fullmatch(str(f)) for f in (facts if isinstance(facts, list) else [])] + list(
            FACT_RE.finditer(str(note or ""))):
        if m and (m.group(1), int(m.group(2))) not in named and m.group(0) not in out:
            out.append(m.group(0))
    return out


def strip_facts(text: str, facts: list[str]) -> str:
    """`text` (a wait note) without the given facts, however the daemon wrote them into it."""
    for f in facts:
        text = text.replace(f" ({f})", "").replace(f"({f}; ", "(").replace(f"; {f}", "").replace(f"{f}; ", "")
    return text


def with_facts(what: str, prev: dict) -> str:
    """A waiting hand-off's `waiting_for`, followed by what the daemon found about the asks and tasks it
    names, so a note rewritten later (its probe says not yet) does not bring back the stale reason."""
    facts = prev.get(FACTS_KEY) if isinstance(prev, dict) else None
    if not isinstance(facts, list) or not facts:
        return what
    return f"{what} ({'; '.join(str(f) for f in facts)})"
