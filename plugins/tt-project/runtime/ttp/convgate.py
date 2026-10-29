# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A ready urgent task never sleeps on a convenience gate.

An urgent task (urgent.is_urgent: priority 1 or better, or an `urgent: <why>` spec line) whose
dependencies are met starts now. Its start gate (a `start_after` delay, a `start_when` probe, a
`landed:#<id>` chain) stays only when the gate's `why` (or the probe itself) names a safety class
(CLASSES): data loss; a spend cap or provider window; a shared device or machine (a lock, hold or
pause, a lull for a restart, upgrade, reboot or power cycle); a charter Restriction; another
person's work; the user's own word (a `test -e <dir>/user-<x>` probe); a real dependency on another
task's new file or symbol. Any other gate is a convenience and is dropped:

- task_add and task_update (coordinator._start_args callers, deferral_update) drop it at once, the
  task starts, and the reply note says so (`dropped_note`). A gate kept for a class records it in
  the `start_safety:<class>` label, so a later priority raise keeps it too.
- Daemon.release_urgent_gates (model-free, every tick, database only: no probe or subprocess)
  applies the same rule to stored queued tasks, so ones parked before an upgrade start on the
  first tick after it: one low observation per release, recorded handled (no coordinator wake).
- Daemon.check_slot_wait raises one event for an urgent task whose dependencies are met, held by
  a kept gate for coordinator.urgent_slot_wait_min while a worker slot is free, naming the gate.

The chain task_add adds for a file or symbol another open task adds (coordinator._overlap_chain) is
the last class by construction: task_add records it, and `tie_class` reads it from the task's overlap
label and its `landed:#<id>` wait, so a task parked before the label existed keeps it too.

A review chained behind its own suite gate (its only unmet dependencies are checks or suite tasks of
the change it reviews: work tasks with no branch, never a code, harness or review task, `suite_waits`) is a convenience too: Daemon.release_urgent_gates drops those
dependencies so the pair runs side by side, with one low observation (`review_suite_gate`), unless
the review's spec names that task's output (a result, log or report it reads).

Other dependencies (depends_on), waiting tasks' retry_when and every hold other than a start gate are
untouched. Accepted gap: the classes are read from words, so a `why` that names a class keeps its
gate whether or not it holds; the coordinator owns that word."""
from __future__ import annotations

import json
import re

from . import urgent

LABEL = "start_safety"   # a kept gate's safety class (one of CLASSES' names); cleared with the deferral
DEPENDENCY = "a dependency on another task's new file or symbol"   # the class of task_add's overlap chain

# (class, words in the gate's why or the probe that name it)
CLASSES = (
    ("data loss", r"\bdata[ -]?loss\b|\blos(?:e|es|ing|t) (?:data|work|state|results?)\b|\bcorrupt\w*|\bbackups?\b"),
    ("spend cap or provider window", r"\bspend\w*|\bbudget\w*|\bcaps?\b|\bquotas?\b|\brate[ -]?limit\w*|\bcredits?\b"
                                     r"|\b(?:provider|usage|billing|token|rate|5[- ]?h(?:our)?) windows?\b"
                                     r"|\blimits? resets?\b|\bttp (?:budget|spend)\b"),
    ("shared device or machine", r"\bdevices?\b|\bmachines?\b|\bbox(?:es)?\b|\bnodes?\b|\bbrokers?\b|\blocks?\b"
                                 r"|\bholds?\b|\bpause[sd]?\b|\blulls?\b|\brestart\w*|\bupgrad\w*|\breboot\w*"
                                 r"|\bpower[ -]?cycl\w*|\breservations?\b|\ballocations?\b|\bmaintenance\b"
                                 r"|\bbroker-released\b|\bdevq\b"),
    ("charter restriction", r"\brestrictions?\b|\bcharter\b"),
    ("another person's work", r"\b(?:another|other) (?:person|people|user|developer|engineer|team|contributor)s?\b"
                              r"|\bsomeone else\b|\bcolleagues?\b"),
    ("the user's word", r"\buser'?s? (?:own )?(?:word|go|ok|okay|approval|answer|decision|call|sign[- ]?off)\b"
                        r"|\buntil the user\b|\b(?:waits?|waiting) (?:for|on) the user\b"
                        r"|\btest -[ef] \S*/user-[\w.-]+"),
    (DEPENDENCY,
     r"(?:#\d+|another task|its producer)\b.*\b(?:new |its )?(?:files?|symbols?|functions?|modules?|helpers?|api"
     r"|class(?:es)?|commands?|flags?|schema|columns?|tables?)\b"
     r"|\b(?:uses?|needs?|imports?|calls?|reuses?|builds? on) #\d+'?s?\b"),
)
_CLASSES = tuple((name, re.compile(rx, re.I)) for name, rx in CLASSES)


def safety_class(*texts: str | None) -> str | None:
    """The first safety class any of `texts` (a gate's why, its probe) names; None for a convenience."""
    for name, rx in _CLASSES:
        if any(t and rx.search(str(t)) for t in texts):
            return name
    return None


def kept_class(labels: list) -> str | None:
    """The `start_safety:<class>` a task's labels recorded, else None."""
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith(LABEL + ":") and lb[len(LABEL) + 1:].strip():
            return lb[len(LABEL) + 1:].strip()
    return None


def tie_class(labels: list, when: str | None) -> str | None:
    """DEPENDENCY when `when` waits on a task landing that the task's overlap label names (the chain
    task_add adds for a new file or symbol another task adds, coordinator._overlap_chain), else None."""
    if not when:
        return None
    from . import landed, overlap
    refs = {x for x in landed.task_refs(when) if str(x).isdigit()}
    named = {x for lb in labels if isinstance(lb, str) and lb.startswith(overlap.LABEL)
             for x in lb[len(overlap.LABEL):].partition(":")[0].split(",")}
    return DEPENDENCY if refs & named else None


def judge(task: dict, after, when: str | None, why: str | None, labels: list | None = None
          ) -> tuple[bool, str | None]:
    """(drop, class) for gate (`after`, `when`, `why`) of `task` (its priority and spec as they will be):
    drop it when the task is urgent, has a gate and no safety class names it; `class` is the one
    found (also for a task that is not urgent, so a later raise keeps the gate)."""
    if not after and not when:
        return False, None
    cls = safety_class(why, when) or kept_class(labels or []) or tie_class(labels or [], when)
    return (urgent.is_urgent(task) and cls is None), cls


def labels_for(cls: str | None, after, when) -> list[str]:
    """The label recording a kept gate's class."""
    return [f"{LABEL}:{cls}"] if cls and (after or when) else []


def gate_words(after, when: str | None, now: float | None = None) -> str:
    """'start_after <time>' and/or 'start_when <probe>', for notes."""
    parts = []
    if after:
        import time as _t
        mins = max(0.0, (float(after) - (_t.time() if now is None else now)) / 60)
        parts.append(f"start_after ({mins:.0f} min away)")
    if when:
        parts.append(f"start_when {str(when)[:160]!r}")
    return " and ".join(parts)


def dropped_note(where: str, task: dict, after, when: str | None) -> str:
    """The reply note when an urgent task's convenience gate was dropped."""
    return (f"{where}: dropped {gate_words(after, when)}: the task is urgent ({urgent.why(task) or 'priority 1'}) and "
            f"the gate's why names no safety class (data loss, a spend cap or provider window, a shared device or "
            f"machine, a charter Restriction, another person's work, the user's word, a dependency on another "
            f"task's new file or symbol), so it starts as soon as its dependencies are met; to keep a gate, give "
            f"`why` naming its class")


def stored(task: dict) -> tuple[list, dict]:
    """A task row's labels and deferral (db.deferral)."""
    from .db import deferral
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        labels = []
    return (labels if isinstance(labels, list) else []), deferral(task)


# A checks or suite task: a work task with no branch (it changes nothing) whose title says so.
SUITE = re.compile(r"\b(?:checks?|suites?|tests?|pytest|ci)\b", re.I)
_OUTPUT = r"\b(?:output|results?|logs?|reports?|numbers|measurements?|artifacts?|findings)\b"


def needs_output(spec: str, tid: int) -> bool:
    """Whether a review's spec reads task `tid`'s output (`#<tid>` and an output word on one line)."""
    return any(re.search(rf"#{tid}\b", ln) and re.search(_OUTPUT, ln, re.I) for ln in (spec or "").splitlines())


def suite_waits(db, review: dict) -> list[int]:
    """A queued review's unmet dependencies when every one is a checks or suite task (a work task with no
    branch, SUITE in its title: a code, harness or review task changes the subject) of the change it reviews (it depends on that task, or names `#<id>` of it) and the
    review needs none's output; [] otherwise."""
    from .db import dependency_ids, review_subject
    if review.get("kind") != "review" or review.get("status") != "queued":
        return []
    subject = review_subject(review)
    unmet = db.unmet_dependencies([review]).get(review["id"]) or []
    if subject is None or not unmet or None in unmet or subject in unmet:
        return []
    for d in unmet:
        t = db.task(d)
        if not t or t["kind"] != "work" or t.get("branch") or not SUITE.search(t["title"] or "") or needs_output(review["spec"], d):
            return []
        if subject not in dependency_ids(t) and not re.search(rf"#{subject}\b", f"{t['title']}\n{t['spec']}"):
            return []
    return unmet
