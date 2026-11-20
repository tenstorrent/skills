# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A plan's start gate: the start condition the user's order for a plan gives ('once X is in place',
'only after Y'), held by every task the plan queues, not only the one it names.

A plan run's hand-off may carry `gate`: {"depends_on": [task ids], "start_when": "<probe>", "why":
"<the order's condition>"}, one or both of depends_on and start_when. The daemon stores it on the plan
task (label `plan_gate:<json>`, `on_handoff`); each done plan hand-off replaces it, one without a gate
clears it. task_add of a task whose spec cites `from plan #<id>` inherits it (`inherit`): its
depends_on joins the task's own, its start_when is ANDed with the task's own, and its why comes first,
verbatim (`gate_why`): it claims the user's word only when it cites a user message itself, so convgate
classes it as any why (an urgent task drops it as a convenience gate unless it names a safety class);
the task gets the HELD label, and lanewait counts no holder a user-cited gate names (`user_holders`). A gate the coordinator records later (task_update
`gate` on the plan, `set_gate`) and a done hand-off's gate also hold the plan's tasks already queued
and never started (`hold_queued`), and name any that already started. A gate it cannot apply
whole (a task id that no longer exists, a combined probe too long) comes back as a note, never
silently: a gate task that failed or was cancelled stays a dependency, so the daemon blocks the
task on it until the coordinator re-points it."""
from __future__ import annotations

import json
import re

LABEL = "plan_gate:"
HELD = "plan_gated:"   # + plan id: a task held on that plan's gate (lanewait: user_holders)
CITE = re.compile(r"\bfrom plan #(\d+)\b", re.I)
_PLAIN = re.compile(r"[|;\n`]|\$\(|(?<!&)&(?!&)")   # a probe that is not one plain `&&` chain


def parse(raw) -> tuple[dict | None, list[str]]:
    """(the gate, problems) of a hand-off's `gate`; (None, []) when it has none."""
    if raw in (None, "", {}, []):
        return None, []
    if not isinstance(raw, dict):
        return None, ["gate must be an object with depends_on and/or start_when"]
    problems, deps = [], []
    rawdeps = raw.get("depends_on") or []
    for d in rawdeps if isinstance(rawdeps, list) else [rawdeps]:
        s = str(d).strip().lstrip("#")
        if s.isdigit():
            deps.append(int(s))
        else:
            problems.append(f"gate depends_on {str(d)[:40]!r} is not a task id")
    when = raw.get("start_when")
    when = " ".join(when.split()) if isinstance(when, str) else ""
    if when.lower() == "now":
        when = ""
    if when:
        from .coordinator import check_probe
        try:
            check_probe(when)
        except ValueError as e:
            problems.append(f"gate start_when: {e}")
            when = ""
    if not deps and not when:
        return None, problems or ["gate names neither depends_on nor start_when"]
    why = " ".join(str(raw.get("why") or "").split())[:160]
    return {"depends_on": list(dict.fromkeys(deps)), "start_when": when, "why": why}, problems


def stored(task: dict | None) -> dict | None:
    """The gate a plan task stores, or None."""
    try:
        labels = json.loads((task or {}).get("labels") or "[]")
    except (ValueError, TypeError):
        return None
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith(LABEL):
            try:
                gate = json.loads(lb[len(LABEL):])
            except ValueError:
                return None
            return gate if isinstance(gate, dict) else None
    return None


def on_handoff(db, task: dict, result) -> tuple[list | None, str]:
    """For a done plan hand-off: (the plan task's new labels, or None when unchanged; the line for its
    event). A gate naming a task that does not exist keeps its other parts, and the line says so."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        labels = []
    labels = labels if isinstance(labels, list) else []
    gate, problems = parse(result.get("gate") if isinstance(result, dict) else None)
    if gate:
        missing = [d for d in gate["depends_on"] if d == task["id"] or not db.task(d)]
        if missing:
            problems.append("gate depends_on " + ", ".join(f"#{d}" for d in missing) + " names no other task here")
            gate["depends_on"] = [d for d in gate["depends_on"] if d not in missing]
            if not gate["depends_on"] and not gate["start_when"]:
                gate = None
    kept = [lb for lb in labels if not (isinstance(lb, str) and lb.startswith(LABEL))]
    new = kept + ([LABEL + json.dumps(gate, ensure_ascii=False, separators=(",", ":"))] if gate else [])
    line = ""
    if gate:
        line = (f"\nStart gate of this plan, held by every task it queues (cite `from plan #{task['id']}` in "
                f"each spec): {words(gate)}")
    if problems:
        line += "\nPlan gate not stored whole: " + "; ".join(problems)
    return (new if new != labels else None), line


def words(gate: dict) -> str:
    """A gate in plain words for a note."""
    parts = []
    if gate.get("depends_on"):
        parts.append("depends_on " + ", ".join(f"#{d}" for d in gate["depends_on"]))
    if gate.get("start_when"):
        parts.append(f"start_when {gate['start_when']}")
    return "; ".join(parts) + (f" ({gate['why']})" if gate.get("why") else "")


def _term(probe: str, p) -> str:
    from . import landed
    cmd = landed.probe_command(probe, p)
    return cmd if cmd != probe else f"({probe})" if _PLAIN.search(probe) else probe


def and_probes(first: str, second: str, p=None) -> str:
    """`first` ANDed with `second` as one probe; '' when the result is no valid probe."""
    if not first or not second:
        return first or second
    from .coordinator import check_probe
    both = f"{_term(first, p)} && {_term(second, p)}"
    try:
        check_probe(both)
    except ValueError:
        return ""
    return both


def held(task: dict) -> bool:
    """Whether a task is held on a plan's gate (HELD), the user's own start condition."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return False
    return any(isinstance(lb, str) and lb.startswith(HELD) for lb in labels if isinstance(labels, list))


def gate_why(pid: int, gate: dict) -> str:
    """A gate's why as a held task's start_why: its own words first, as given, then the plan it came from.
    It adds no claim of the user's word: convgate classes it by what the why itself cites."""
    return f"{gate['why']} (plan #{pid}'s start gate)" if gate.get("why") else f"plan #{pid}'s start gate"


def user_holders(db, task: dict) -> set[int]:
    """The task ids the user-given gates a task is HELD on name (their depends_on, the tasks their
    start_when waits on): lanewait counts none of them. A gate whose why and probe cite no user message
    names none, and the task's other holders always count."""
    from . import convgate, landed
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return set()
    out: set[int] = set()
    for lb in labels if isinstance(labels, list) else []:
        if not (isinstance(lb, str) and lb.startswith(HELD) and lb[len(HELD):].isdigit()):
            continue
        gate = stored(db.task(int(lb[len(HELD):])))
        if gate and convgate.user_word(gate.get("why"), gate.get("start_when")):
            out |= {d for d in gate.get("depends_on") or [] if isinstance(d, int)}
            out |= {int(r) for r in landed.task_refs(gate.get("start_when") or "") if r.isdigit()}
    return out


def inherit(p, db, a: dict) -> tuple[dict, list[int], list[str], list[str]]:
    """A task_add `a` under the gates of the plans its spec cites: (the action with the gates' probes
    ANDed into its start_when and their whys first in its why; the gates' depends_on, for the caller to
    add after its own dead-dependency check; notes; labels marking it HELD). An action citing no gated
    plan comes back as is."""
    deps, notes, whys, marks = [], [], [], []
    when = a.get("start_when")
    when = "" if not isinstance(when, str) or when.strip().lower() == "now" else when.strip()
    gated = False
    for pid in dict.fromkeys(int(x) for x in CITE.findall(str(a.get("spec") or ""))):
        plan = db.task(pid)
        gate = stored(plan) if plan and plan["kind"] == "plan" else None
        if not gate:
            continue
        gated = True
        marks.append(f"{HELD}{pid}")
        for d in gate.get("depends_on") or []:
            if not isinstance(d, int) or not db.task(d):
                notes.append(f"plan #{pid}'s gate waits on #{d}, which names no task here: not applied; "
                             f"add the dependency it stands for with task_update")
            elif d not in deps:
                deps.append(d)
                if dead := db.dead_dependency([d]):
                    notes.append(f"plan #{pid}'s gate waits on #{d}, which is {dead[1]}: the task is blocked on it "
                                 f"until you re-point depends_on to the work that meets the gate")
        if g := str(gate.get("start_when") or ""):
            if both := and_probes(g, when, p):
                when = both
            else:
                notes.append(f"plan #{pid}'s gate start_when {g[:120]!r} could not be ANDed with the task's own "
                             f"start_when (too long or invalid together): only the gate's is kept; resend the "
                             f"task's own check with task_update, shorter, ANDed with the gate's")
                when = g
        whys.append(gate_why(pid, gate))
    if not gated:
        return a, [], [], []
    out = dict(a)
    if when:
        out["start_when"] = when
    if when and whys:
        out["why"] = "; ".join([*whys, *([str(a["why"])] if a.get("why") else [])])
    return out, deps, notes, marks


def set_gate(p, db, plan: dict, raw) -> list[str]:
    """task_update `gate` on plan task `plan`: store it (an empty one clears it) and hold every task
    already queued from the plan on it (hold_queued); notes."""
    gate, problems = parse(raw)
    labels, line = on_handoff(db, plan, {"gate": raw} if gate else {})
    if gate is None and problems and raw not in (None, "", {}, []):
        raise ValueError("; ".join(problems))
    if labels is not None:
        db.update_task(plan["id"], labels=labels)
    notes = [x for x in line.strip().splitlines() if x.startswith("Plan gate not stored")]
    if gate := stored(db.task(plan["id"])):
        notes += hold_queued(p, db, plan["id"], gate)
    return notes


def hold_queued(p, db, pid: int, gate: dict) -> list[str]:
    """Hold every task that cites `from plan #<pid>`, has never started and is not yet HELD on it, on
    `gate`, as task_add would have (a task queued in the same batch before the gate was recorded);
    notes, also naming any such task already started (the coordinator steers or stops it)."""
    from . import landed
    from .db import deferral, dependency_ids, without_deferral
    from .coordinator import defer_labels, START_WHY_CHARS
    notes, mark = [], f"{HELD}{pid}"
    rows = db.q("SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled') AND id != ? "
                "AND spec LIKE ?", (pid, f"%plan #{pid}%"))
    for t in rows:
        if int(pid) not in {int(x) for x in CITE.findall(t["spec"] or "")}:
            continue
        try:
            labels = json.loads(t["labels"] or "[]")
        except (ValueError, TypeError):
            labels = []
        labels = labels if isinstance(labels, list) else []
        if mark in labels:
            continue
        if t["status"] not in ("queued", "blocked") or t["attempts"]:
            notes.append(f"#{t['id']} cites plan #{pid} but has already started, so its gate cannot hold it: "
                         f"steer or stop it if it must wait ({words(gate)})")
            continue
        d = deferral(t)
        deps = dependency_ids(t)
        deps += [x for x in gate.get("depends_on") or [] if x not in deps and x != t["id"] and db.task(x)]
        when, own = d.get("when") or "", d.get("when") or ""
        g = str(gate.get("start_when") or "")
        if g and str(t["id"]) in landed.task_refs(g):   # it would wait on its own landing for ever
            notes.append(f"#{t['id']}: plan #{pid}'s gate start_when {g[:120]!r} waits on #{t['id']} itself: "
                         f"not applied to it")
        elif g:
            when = and_probes(g, own, p) or g
            if own and when == g:
                notes.append(f"#{t['id']}: plan #{pid}'s gate start_when could not be ANDed with its own "
                             f"start_when: only the gate's is kept; resend its own with task_update")
        why = "; ".join([gate_why(pid, gate), *([d["why"]] if d.get("why") else [])])
        new = without_deferral(labels) + defer_labels(d.get("after"), when, " ".join(why.split())[:START_WHY_CHARS]
                                                       if when else None) + [mark]
        db.update_task(t["id"], depends_on=deps, labels=new)
        notes.append(f"#{t['id']} cites plan #{pid}: now held on its gate ({words(gate)})")
    return notes
