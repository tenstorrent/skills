# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Facts at send time: the live record of the tasks a spec or a note names.

A coordinator's hand-back spec, or a worker's note to another project (`ttp note --to`), often
states another task's status ("#12's fix is not pushed yet", "the switch is not live yet") from
memory, and is stale by the time it is read. So the text is not the authority: each task it names
(`#<id>` with no other noun before it, or `task #<id>`; not a message, PR, issue, ledger entry or
other numbered item, and every number of a run like `#1, #2 and #3` takes the noun leading it) gets
one line read from the records when the text is sent or dispatched: its status, the commit the push
queue, a detached push or a supersession recorded for it or a task carrying it on (landed.task_shas),
a push still queued, and the machine changes it recorded in the change ledger (machine_ledger). A
number an unknown word leads gets its line as `task #<id> (if meant)`. The worker prompt carries
them as a block (prompt_block, built at dispatch), a note to another project as a suffix
(note_suffix, at send). Workers check stated status against them before acting: the records win.
"""
from __future__ import annotations

import re
import time

MAX_TASKS = 6          # tasks named in one text that get a line
TITLE_CHARS = 70
WHAT_CHARS = 100
LINE_CHARS = 360
FACTS_MARK = " [facts at send time from "   # note_suffix's block; a reader strips it from the sender's own words
# `#<id>` as a task reference; the word before it (or before the first number of its run, as in
# `ledger #1, #2 and #3`) says when it is another kind of number. A word not known either way is
# probably a noun naming something else: its line reads `task #<id> (if meant)`.
_REF = re.compile(r"(?<![\w#&])#(\d{1,7})(?!\d)")
_RUN_GAP = re.compile(r"\s*(?:,\s*(?:and|or|&)?|and|or|&|/|\+)\s*", re.I)
_WORD_BEFORE = re.compile(r"([A-Za-z][\w'-]*)([.:]?)\s*$")
_IS_TASK = re.compile(r"(?:tasks?|code|reviews?|plans?|questions?|work|research)", re.I)
_NOT_TASK = re.compile(r"(?:msgs?|messages?|prs?|pulls?|requests?|issues?|changes?|entry|entries|ledgers?|notes?|"
                       r"steps?|items?|rows?|lines?|comments?|no|nos|num|number|numbers|batch(?:es)?|asks?|alerts?|"
                       r"holds?|commits?|runs?|jobs?|tickets?|bugs?|slots?|chips?|tray(?:s)?|attempts?|wakes?|goals?|"
                       r"reservations?|events?|points?|options?|phases?|rounds?)", re.I)
# Words that do not name what the number is: a bare `#<id>` after them is a task, as before.
_PLAIN = frozenset("""a an the its their this that these those and or but nor so then also too both either not
    see per cf vs re via in on of by for to from with without into onto at as after before since until than
    like about over under is was are were be been being has have had do does did if when while once whether
    fix fixes fixed push pushes pushed pushing land lands landed landing rebase rebases rebased review reviewed
    carry carries carried supersede supersedes superseded block blocks blocked unblock unblocks unblocked
    replace replaces replaced redo redoes redid rerun reruns retry retries retried resume resumes resumed
    follow follows followed finish finishes finished continue continues continued close closes closed
    reopen reopens reopened drop drops dropped keep keeps kept wait waits waiting waited need needs needed
    run ran use uses used check checks checked merge merges merged revert reverts reverted ship ships shipped
    cancel cancels cancelled canceled restart restarts restarted start starts started undo undoes undid
    done only now still yet just again plus it we they you i my our your his her them us all any each every
    which who what""".split())


def refs(text: str, skip=()) -> list[tuple[int, bool]]:
    """The task ids `text` names as `#<id>`, in order, once each, without `skip`, at most MAX_TASKS, each
    with whether the text clearly means a task (False: an unknown noun leads it, so 'if meant')."""
    text = text or ""
    out: list[tuple[int, bool]] = []
    seen: set[int] = set()
    kind, prev = "task", None
    for m in _REF.finditer(text):
        if prev is None or not _RUN_GAP.fullmatch(text[prev.end():m.start()]):
            w = _WORD_BEFORE.search(text[max(0, m.start() - 40):m.start()])
            word = w.group(1).lower() if w else ""
            # After `.` or `:` only a noun of another kind leads the number (`msg. #3`, `entries: #3`).
            kind = ("other" if word and _NOT_TASK.fullmatch(word)
                    else "task" if not word or w.group(2) or word in _PLAIN or _IS_TASK.fullmatch(word)
                    else "unclear")
        prev = m
        n = int(m.group(1))
        if kind == "other" or n in seen or n in skip:
            continue
        seen.add(n)
        out.append((n, kind == "task"))
    return out[:MAX_TASKS]


def ids_in(text: str, skip=()) -> list[int]:
    """The task ids `text` names as `#<id>` (see refs)."""
    return [n for n, _ in refs(text, skip)]


def _clip(text, chars: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= chars else text[:chars - 1] + "…"


def _queued(p, carry) -> str:
    """The newest push queue row of `carry` (the task and its carriers) not pushed yet, as a few words."""
    try:
        rows = p.db.q("SELECT task, status FROM push_queue WHERE task IN (%s) ORDER BY id DESC LIMIT 1"
                      % ",".join("?" * len(carry)), list(carry))
    except Exception:
        return ""
    if not rows or rows[0]["status"] in ("pushed", "landed"):
        return ""
    r = rows[0]
    return f"push queue: {r['status']}" + (f" (through #{r['task']})" if r["task"] != min(carry) else "")


def _changes(p, tid: int) -> list[str]:
    from . import machine_ledger
    try:
        rows = machine_ledger.changes(open_only=False)
    except Exception:
        return []
    out = []
    for c in rows:
        if c.get("by") != p.name or str(c.get("task")) != str(tid):
            continue
        state = "open" if not c.get("closed") else f"closed: {_clip(c.get('closed_why') or 'closed', 40)}"
        out.append(f"machine change #{c.get('id')} on {c.get('alias')} ({state}): {_clip(c.get('what'), WHAT_CHARS)}")
    return out[-3:]


def fact_line(p, tid: int) -> str:
    """One line of task `tid`'s live record ('' when there is no such task)."""
    from . import landed
    try:
        t = p.db.task(int(tid))
    except Exception:
        return ""
    if not t:
        return ""
    parts = [f"#{tid} {_clip(t.get('title'), TITLE_CHARS)} ({t.get('kind')}, {t.get('status')})"]
    try:
        shas, why = landed.task_shas(p, int(tid))
    except Exception:
        shas, why = [], ""
    if shas and not why.startswith("its branch"):
        parts.append(f"on the push branch as {', '.join(s[:10] for s in shas[-3:])} ({why})")
    else:
        queued = _queued(p, landed.carriers(p.db, int(tid)))
        parts.append(queued or "no push or landing recorded")
    try:
        sup = landed.superseded(p.db, [int(tid)]).get(int(tid), [])
    except Exception:
        sup = []
    if sup and not (shas and why.startswith("the commit recorded")):
        parts.append("superseded by " + ", ".join(s[:10] for _, s in sup[-2:]))
    parts += _changes(p, int(tid))
    return _clip("; ".join(parts), LINE_CHARS)


def lines(p, text: str, skip=()) -> list[str]:
    out = []
    for i, sure in refs(text, skip):
        x = fact_line(p, i)
        if x:
            out.append(x if sure else f"task {x[:len(str(i)) + 1]} (if meant){x[len(str(i)) + 1:]}")
    return out


def prompt_block(p, task: dict, now: float | None = None) -> str:
    """The worker prompt's block for the other tasks its title and spec name ('' if none)."""
    from . import timefmt
    try:
        got = lines(p, f"{task.get('title') or ''}\n{task.get('spec') or ''}", skip={int(task.get("id") or 0)})
    except Exception:
        return ""
    if not got:
        return ""
    at = timefmt.short(time.time() if now is None else now, p, now)
    return ("## Facts at send time\n"
            f"Read from this project's task records and machine change ledger at {at}, when this task was "
            "dispatched. The spec was written earlier: where it states one of these tasks' status (pushed, "
            "landed, superseded, a machine change made or live), check it against these lines before acting. "
            f"The records win; recheck with `ttp landed --task <id>` or `ttp task {p.name} show <id>`.\n"
            + "\n".join(f"- {x}" for x in got))


def note_suffix(p, text: str, limit: int) -> str:
    """`text` (a note to another project, one line) with its named tasks' live records appended, as many
    as fit within `limit` characters; `text` itself when none do or nothing is named."""
    try:
        got = lines(p, text)
    except Exception:
        return text
    while got:
        out = f"{text}{FACTS_MARK}{p.name}'s records: {' | '.join(got)}]"
        if len(out) <= limit:
            return out
        got.pop()
    return text
