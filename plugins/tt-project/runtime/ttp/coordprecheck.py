# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A session coordinator's answer checked before its turn ends (the always-on coordinator session).

The coordinator's PreToolUse hook (coordtools.pre_tool_use) runs `refusal` on the turn's
StructuredOutput call. An action coord.apply would refuse (an unknown task id, a dependency that can
no longer finish, a task cap reached, a field a running or finished task keeps) is denied with its
reason as the tool result, so the model fixes or drops it in the same turn instead of learning it at
the next wake. `problems` only reads: it writes no task, kv row or file of the project. Actions that
pass are applied after the turn as before, and coord.apply still runs every check: this one is a
subset, and a refusal it misses is carried to the next wake as before (coordinator.REJECTED_KEY).
Its one write to the project: a task_add denied on a full 24 h task cap asks the same retry wake at the
cap's next free slot as apply's refusal does (coordinator.slot_wake), so the denial says to drop it, not
to resend it.

It never stops a turn from ending. An action resent unchanged after a denial goes through (apply
refuses it with its usual side effects, such as the retry wake of a task cap), at most MAX_DENIALS
answers per run are denied (one: the fixed resend applies, a second refusal is apply's, as before),
and any error lets the answer through (apply is the real gate). The denied action hashes and the
reasons are kept in the run dir (STATE_FILE), not in the project: the daemon reads them when it records
the turn's rejections (Daemon._record_rejections), tallies both (rejtally.py, `ttp stats`) and starts no
turn of its own for rejections the turn was already shown and retried. coordinator.answer_precheck
false turns the check off (every answer goes straight to apply). Providers without the answer hook
(Codex, Cursor, chat) get the equivalent second pass after the turn (after_turn): apply's refusals go back
to the same session at once, once, and what it refuses again wakes no turn of its own."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

MAX_DENIALS = 1   # denied answers per run; past it every answer goes through (task #8429)
STATE_FILE = "answer-precheck.json"   # in the run dir: {"denials": n, "seen": [action hashes], "caught": [reasons]}
SWITCH = "answer_precheck"   # coordinator.<SWITCH>: false turns the check off
REQUEUE = ("queued", "blocked", "cancelled", "done")   # the statuses a task_update sets (coord.apply)


def _key(a: dict) -> str:
    return hashlib.sha256(json.dumps(a, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def problems(p, actions: list, wakes: list | None = None) -> list[tuple[int, str]]:
    """(action index, why) of each action coord.apply would refuse, by the checks it can make without
    a side effect; [] when it finds none. Reads the project's tasks and config only. `wakes` (a list):
    gets (free_at, review, why) of each task_add refused on a full cap, for refusal to ask the retry wake."""
    from . import coordinator as coord, obligations, timefmt
    from .db import TERMINAL_TASK_STATES, _first_dead, dependency_ids
    from .providers.base import normalize_action
    db, cfg, out = p.db, p.config(), []
    home = timefmt.home(p)
    # A config_set in the batch is applied first and may change a cap: the caps are left to apply then.
    caps = not any(isinstance(a, dict) and a.get("type") == "config_set"
                   and "tasks_per_day" in str(a.get("key") or "") for a in actions)
    left: dict[bool, int] = {}
    # The statuses this batch's task_updates set: apply runs them in order, so a dependency requeued
    # or a duplicate cancelled earlier in the batch counts as such (no refusal apply would not make).
    states = {r["id"]: r["status"] for r in db.q("SELECT id, status FROM tasks")}

    def dead(deps: list):
        return _first_dead(deps, states)
    for i, raw in enumerate(actions):
        if not isinstance(raw, dict):
            continue
        try:
            a = normalize_action(raw)
        except ValueError as e:
            out.append((i, str(e)))
            continue
        t = a.get("type")
        try:
            if t == "task_update":
                try:
                    task = db.task(int(a.get("id")))
                except (TypeError, ValueError):
                    raise ValueError("task_update needs the id of a task") from None
                if not task:
                    raise ValueError(f"no task #{a.get('id')}")
                tid, status = task["id"], a.get("status")
                if status == "waiting":
                    raise ValueError(f"#{tid} rejected: {coord.NO_WAITING_STATUS}")
                if task["status"] == "running":
                    if status in REQUEUE and status != "cancelled":
                        raise ValueError(f"task #{tid} is running: send `spec` alone to steer it mid-run, "
                                         f"or set status cancelled")
                    if a.get("resources") is not None:
                        raise ValueError(f"task #{tid} is running: its resources change only while it is not; "
                                         f"cancel it and re-add it with `continues` to move it now")
                if task["status"] in ("running", *TERMINAL_TASK_STATES):
                    if a.get("pr_branch") is not None:
                        raise ValueError(f"task #{tid} is {task['status']}: its branch changes only before it "
                                         f"starts; add a new one with continues and pr_branch \"\"")
                    if a.get("start_after") is not None or a.get("start_when") is not None:
                        raise ValueError(f"task #{tid} is {task['status']}: only a task that has not started "
                                         f"can be deferred; add a new one with start_after/start_when")
                deps = dependency_ids(task)
                new_status = status if status in REQUEUE else task["status"]
                if a.get("depends_on") is not None:
                    deps = coord._new_dependencies(db, task, a["depends_on"])
                    if status not in REQUEUE and task["status"] == "blocked" and dead(dependency_ids(task)):
                        new_status = "queued"   # apply releases a task blocked on a dead dependency
                if new_status == "queued" and (status in REQUEUE or a.get("depends_on") is not None):
                    if found := dead(deps):
                        dep, why = found
                        raise ValueError(f"#{tid} rejected: depends on #{dep} which "
                                         f"{'does not exist' if why == 'does not exist' else 'is ' + why}; "
                                         f"drop or replace depends_on")
                states[tid] = new_status
            elif t == "task_add":
                title = str(a.get("title") or "").strip()
                missing = [f for f, v in (("title", title), ("spec", str(a.get("spec") or "").strip())) if not v]
                if missing:
                    raise ValueError(f"{coord.MISSING_FIELDS} {' and a '.join(missing)}: title and spec must "
                                     f"both be non-null text")
                kinds = coord.task_kinds(p)
                if (a.get("kind") or "work") not in kinds:
                    raise ValueError(f"{coord.UNKNOWN_KIND} {str(a.get('kind'))[:40]!r} is not one of "
                                     f"{', '.join(sorted(kinds))}")
                dup = [r["id"] for r in db.q("SELECT id FROM tasks WHERE title=?", (title,))
                       if states.get(r["id"]) not in TERMINAL_TASK_STATES]
                if dup and str(dup[0]) != str(a.get("continues")):
                    raise ValueError(f"duplicate of open task #{dup[0]}")
                deps = coord._new_dependencies(db, None, a.get("depends_on") or [])
                if a.get("continues") is not None:
                    try:
                        old = db.task(int(a["continues"]))
                    except (TypeError, ValueError):
                        raise ValueError("task_add continues must be a task id") from None
                    if not old:
                        raise ValueError(f"task_add continues: no task #{a['continues']}")
                    if states.get(old["id"]) == old["status"]:   # not requeued or cancelled earlier in the batch
                        coord._continued(db, old["id"], deps)
                if found := dead(deps):
                    raise ValueError(f"task_add rejected: depends on #{found[0]} which is {found[1]}; "
                                     f"drop or replace depends_on")
                if caps:
                    review = a.get("kind") == "review"
                    cap = coord.task_cap(cfg, review)
                    if review not in left:
                        left[review] = cap - len(coord.tasks_made(db, time.time() - 86400, review)) if cap > 0 else 0
                    if left[review] <= 0:
                        what = "review tasks" if review else "new tasks (reviews have their own cap)"
                        if cap <= 0:
                            raise ValueError(f"the cap on {what} is 0: none can be added until it is raised; "
                                             f"drop it")
                        free_at = coord.next_task_slot(db, cap, review=review)
                        if free_at is None:   # this batch's own task_adds fill it: no slot time yet
                            raise ValueError(f"the 24 h cap of {cap} {what} is full with this batch's earlier "
                                             f"task_adds; drop it or one of them")
                        why = f"the 24 h cap of {cap} {what} is full; the next slot frees at {coord._clock(free_at, p)}"
                        if wakes is not None:
                            wakes.append((free_at, review, f"task_add {title!r}: {why}"))
                        raise ValueError(f"{why}: drop it from this answer; you are woken then to add it if it is "
                                         f"still needed (STATE's budget lines show the caps)")
                    left[review] -= 1
            elif t == "sweep":
                _sweep(p, db, a, home)
            elif t in ("reply", "notify") and a.get("claims"):
                obligations.check_claims(a["claims"], p)
            elif t == "obligation":   # the times apply parses: a new order's, a due move's (`key` + `due`)
                from . import coordsession, relay
                said = [a.get(f) for f in ("text", "evidence", "why", "justify")] + [
                    m.get("evidence_required") for m in a.get("milestones") or [] if isinstance(m, dict)]
                if forged := [d for x in said if isinstance(x, str)
                              for d in coordsession.checked(db, x, relay.agent_orders(cfg), strict=False)[1]]:
                    raise ValueError(f"obligation rejected: a line labelled as the user's words fails the verbatim "
                                     f"check: {forged[0][1]}. Quoted text on such a line must be that message's own "
                                     f"words, with its msg #N; put names and terms in backticks, or record it "
                                     f"without the user's-words label")
                key = str(a.get("key") or "").strip()
                if a.get("order") is not None and not key:
                    times = ("due", "start_by")
                elif a.get("order") is None and key and not a.get("owners") \
                        and not any(str(a.get(f) or "").strip() for f in ("evidence", "why")):
                    times = ("due",)
                else:
                    times = ()
                for f in times:
                    if a.get(f) not in (None, ""):
                        obligations._when(a[f], f"{key or 'order'} {f}", p)
        except ValueError as e:
            out.append((i, str(e).replace(f" {coord.INFO_MARK}", "")))
    return out


def _sweep(p, db, a: dict, home: str) -> None:
    """The checks sweep.record makes of one `sweep` outcome before it writes anything."""
    from . import coordinator as coord, sweep
    if str(a.get("outcome") or "") not in sweep.OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(sweep.OUTCOMES)}")
    key = sweep._item_key(a)
    if key.startswith("task:") and not db.task(int(key[5:])):
        raise ValueError(f"no task #{key[5:]}")
    if a.get("outcome") != "still_blocked" or sweep._finished(db, key):
        return
    if not sweep.recheckable({"until": a.get("until") or a.get("reason") or "", "probe": a.get("until_probe") or ""}):
        raise ValueError(f"{key} still_blocked needs a re-checkable condition: `until_probe` (a shell probe) or "
                         f"`until` naming what must change (not 'keep it blocked')")
    at = coord.parse_start_after(a.get("start_after") or "now", tz=home)
    if not at or at <= time.time():
        raise ValueError(f"{key} still_blocked needs `start_after`: when to check it next")


def refusal(payload: dict) -> str | None:
    """Why the session coordinator's StructuredOutput call is denied (None: it goes through).
    Fails open: no project, no run dir or any error lets the answer through (module doc)."""
    try:
        answer = payload.get("tool_input")
        actions = answer.get("actions") if isinstance(answer, dict) else None
        base, run_dir = os.environ.get("TTP_PROJECT"), os.environ.get("TTP_RUN_DIR")
        if not actions or not isinstance(actions, list) or not base or not run_dir:
            return None
        state_path = Path(run_dir) / STATE_FILE
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        denials, seen = int(state.get("denials") or 0), set(state.get("seen") or [])
        if denials >= MAX_DENIALS:
            return None
        from .project import Project
        proj = Project(base)
        if not (proj.state / "project.db").is_file():   # opening one would create it
            return None
        if not enabled(proj.config()):
            return None
        wakes: list = []
        found = [(i, why) for i, why in problems(proj, actions, wakes)
                 if _key(actions[i]) not in seen]   # resent unchanged: apply decides (module doc)
        if not found:
            return None
        if wakes:   # a task_add denied on a full cap: woken at the next slot as apply's refusal would
            from . import coordinator as coord
            coord.slot_wake(proj.db, *min(wakes))
        seen |= {_key(actions[i]) for i, _ in found}
        from .project import durable_write
        caught = list(state.get("caught") or []) + [f"{actions[i].get('type')}: {why}"[:300] for i, why in found]
        durable_write(state_path, json.dumps({"denials": denials + 1, "seen": sorted(seen), "caught": caught}))
        lines = "; ".join(f"actions[{i}] ({actions[i].get('type')}): {why}" for i, why in found)
        return (f"this answer was not applied. {lines}. Fix or drop each of these and send the whole answer "
                f"again, once; the other actions are fine. The next answer is not checked here: what apply "
                f"still refuses then is refused after the turn and shown in the next digest")
    except Exception:
        return None


def enabled(cfg: dict) -> bool:
    """coordinator.answer_precheck (default on)."""
    v = (cfg.get("coordinator") or {}).get(SWITCH, True)
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


def retried(run_dir) -> list[str]:
    """The reasons the run's answer was denied for in its turn ([] when it was not): those actions came
    back once to fix, so their refusal after the turn needs no turn of its own (Daemon._record_rejections)."""
    try:
        state = json.loads((Path(run_dir) / STATE_FILE).read_text())
    except (OSError, ValueError, TypeError):
        return []
    return [str(x) for x in state.get("caught") or []] if int(state.get("denials") or 0) else []


# Providers whose coordinator answer is a StructuredOutput tool call the PreToolUse hook sees (refusal).
HOOKED = ("claude",)
PASS_KEY = "answer_precheck_pass"   # kv {"at": ts, "caught": [reasons]}: the second pass queued for a hook-less turn


def after_turn(p, r: dict, problems: list[str], now: float | None = None) -> list[str]:
    """The equivalent second pass for a provider without the answer hook (Codex, Cursor, chat): their
    answer ends the turn with no tool call to deny, so apply's refusals of a turn are sent back to the
    same session at once, once, as a queued event (the session resumes with them), not after
    coord.REJECTION_WAIT_S. Returns the reasons when this turn was that pass ([] otherwise), which
    Daemon._record_rejections treats like a hooked turn's retry: what apply refuses again wakes none."""
    try:
        if str(r.get("provider") or "claude") in HOOKED or not enabled(p.config()):
            return []
        from . import coordinator as coord
        db, now = p.db, now or time.time()
        pend = db.kv(PASS_KEY)
        if isinstance(pend, dict) and float(r.get("started") or 0) >= float(pend.get("at") or 0):
            db.x("DELETE FROM kv WHERE key=?", (PASS_KEY,))
            return [str(x) for x in pend.get("caught") or []]
        fix = [str(x) for x in problems or [] if not coord.informational_rejection(str(x))]
        if not fix:
            return []
        with db.tx():
            db.set_kv(PASS_KEY, {"at": now, "caught": [x[:300] for x in fix]})
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (now, "daemon", "answer_precheck_pass", "normal",
                  ("Your last answer was applied except these actions, which were refused: " + "; ".join(fix)
                   + ". Fix or drop each and send only those again, once.")[:1500], "queued"))
        return []
    except Exception:   # bookkeeping: without it the refusals reach the next digest as before
        return []
