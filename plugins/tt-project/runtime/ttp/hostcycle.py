# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Planned host cycles: a reboot or power cycle of this host that a recovery owner chose to make.

Whoever cycles the host on purpose records it first, in <project state>/host-cycle/<id>/record.json
(`ttp drain <name> --reboot "<why>"` writes one): the boot it started on (`boot_id`), when
(`started`, epoch seconds), why (`reason`) and `dry_run` when nothing was cut. On the next boot
every project on this host reads the records of every project here, so a cycle another project
made counts too. A reboot that matches one was not caused by a task's detached job: that job's
loss does not count toward max_reboot_losses.

`ttp drain` also lists the project's `ttp detach` jobs still running here, which a reboot would
end, and the record names them as interrupted so the woken tasks and the user can see why.

Holding the host. As it starts, `ttp drain --reboot` also writes a power-cycle intent for this host
into the shared machine ledger (machine_ledger.add_power_cycle, at now + its wait, `hold_from` now),
unless one made on this boot is due already (open_intent: it is reused, and pushed on to now + the
wait when it was due sooner). While an intent for this host is due (hold: its cycle_window, so a
drain's intent holds from when it was written), every project's daemon here starts no worker or
reviewer run and no push-queue push, and `ttp checks` starts nothing new; the coordinator still
runs, and runs in flight go on and are told to hand off (daemon.power_cycle_notices). Once the drain
records the cycle, the intent's time moves to then (cycle_now). The hold lifts once the intent
closes (a no-reboot record from the drain that wrote it closes it, and `ttp resume` closes one its own
drain wrote but never recorded: release), once this host booted again, or POWER_CYCLE_LEAD_S after
its time (lapsed: each daemon posts one low feed line). A no-reboot record from a drain that only took
the intent up never closes it: the cycle it was written for is still coming. Instead the intent gets
back the time and window it had before the take-ups moved it, once every drain that moved it since
then has said no reboot (while one still waits, or its cycle is coming, its time stands), so it holds
again only in its own window (_undo_takeups).
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from pathlib import Path

from . import project
from .project import Project, write_json

FOLDER = "host-cycle"   # under a project's state
# A record matches a boot that came at most this long after it started: a record left by a cycle
# that never happened does not excuse a crash hours later on the same boot.
WINDOW_S = 7200


def _state_dirs(p: Project | None) -> list[Path]:
    """This project's state folder first, then every other registered project's on this host."""
    return [d for _, d in _named_state_dirs(p)]


def _named_state_dirs(p: Project | None) -> list[tuple[str, Path]]:
    """(project name, state folder) pairs, this project first, then the other registered ones here."""
    out = [(p.name, p.state)] if p else []
    seen = {d for _, d in out}
    here = project.hostname()
    for name, e in sorted((project.load_registry().get("projects") or {}).items()):
        if isinstance(e, dict) and e.get("host") in (None, here) and e.get("dir"):
            d = Path(e["dir"]) / project.FOLDER / "state"
            if d not in seen:
                seen.add(d)
                out.append((str(name), d))
    return out


def planned(p: Project | None, boot: str | None, boot_at: float | None) -> dict | None:
    """The record of a planned cycle that ended boot `boot` (the earlier boot), if one says so: not a
    dry run, started on that boot, and at most WINDOW_S before `boot_at` (this boot's start)."""
    if not boot:
        return None
    for owner, d in _named_state_dirs(p):
        try:
            files = sorted((d / FOLDER).glob("*/record.json"), reverse=True)
        except OSError:
            continue
        for f in files:
            try:
                r = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(r, dict) or r.get("boot_id") != boot or r.get("dry_run") or r.get("rebooted") is False:
                continue
            started = r.get("started")
            if boot_at and (not isinstance(started, (int, float)) or not 0 <= boot_at - started <= WINDOW_S):
                continue
            return {**r, "path": str(f), "project": owner}
    return None


def running_jobs(p: Project, days: float = 7) -> list[dict]:
    """The project's `ttp detach` jobs still running (or queued for the daemon) on this boot, and its live
    `ttp host-run --detach` runs (hostrun.live_detached): what a reboot of this host would end. A job driven
    by `ttp detach --remote` lives on the other host."""
    from . import hostrun
    from .detach import state
    from .runner import boot_ids
    boots = set(boot_ids())
    out = []
    for r in p.db.q("SELECT id, task, dir FROM runs WHERE started>? AND dir IS NOT NULL ORDER BY id",
                    (time.time() - days * 86400,)):
        try:
            jobs = json.loads((Path(r["dir"]) / "detached.json").read_text())
        except (OSError, ValueError):
            continue
        for j in jobs if isinstance(jobs, list) else []:
            if not isinstance(j, dict) or not j.get("rc") or (j.get("boot") and j["boot"] not in boots):
                continue
            if state(Path(j["rc"]))[0] in ("running", "queued"):
                out.append({"name": j.get("name"), "run": r["id"], "task": r["task"], "rc": j["rc"],
                            "log": j.get("log"), "command": j.get("command")})
    return out + hostrun.live_detached(p)


def record(p: Project, reason: str, left_running: list[dict], jobs: list[dict], intent: int | None = None,
           intent_was: dict | None = None, since: float | None = None) -> Path:
    """Record a planned cycle of this host, about to happen: the next boot matches it (planned).
    `intent`: the machine-ledger power-cycle intent it was announced with (open_intent); `intent_was`: that
    intent's time fields before this drain took it up and moved them (before), so a record that says no
    reboot can put them back (hold); `since`: when the drain started, so its edits of the intent are told
    from others' (_undo_takeups)."""
    from .runner import boot_id
    now = time.time()
    rid = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    d = p.state / FOLDER / rid
    n = 1
    while d.exists():   # two drains within a second
        n += 1
        d = p.state / FOLDER / f"{rid}-{n}"
    d.mkdir(parents=True)
    f = d / "record.json"
    write_json(f, {"id": d.name, "started": now, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                   "boot_id": boot_id(), "dry_run": False, "reason": reason, "by": "ttp drain",
                   "left_running": left_running, "interrupted": jobs, "intent": intent, "project": p.name}
               | ({"intent_was": intent_was} if intent_was is not None else {})
               | ({"since": since} if since is not None else {}))
    return f


def describe(jobs: list[dict]) -> str:
    return ", ".join(f"{j['name']} (task #{j['task']}, log {j['log']})" if j.get("task") else
                     f"{j['name']} (run {j['run']}, log {j['log']})" for j in jobs)


# Holding new runs before a planned cycle (see the module docstring) ------------------------------
def this_host() -> str | None:
    """This host's alias in `ttp machines list`, or None when it has no entry there."""
    from . import machines
    hit = machines.here()
    return hit[0] if hit else None


def _intents(alias: str) -> list[dict]:
    from . import machine_ledger as ml
    return [c for c in ml.changes([alias]) if c.get("kind") == ml.POWER_CYCLE and isinstance(c.get("at"), (int, float))]


def _rebooted_since(c: dict) -> bool:
    """Whether this host booted again since intent c was made."""
    from .runner import boot_time, same_boot
    if c.get("boot"):
        return not same_boot(c["boot"])
    bt = boot_time()
    return bool(bt) and bt > float(c.get("added") or 0)


def open_intent(p: Project, reason: str, wait_s: float, now: float | None = None) -> tuple[dict | None, bool, str]:
    """`ttp drain --reboot` as it starts: the open power-cycle intent for this host (at now + wait_s,
    holding from now), whether this call wrote it, and a line to print. An open one made on this boot
    and due now is reused, pushed on to now + wait_s when it was due sooner and held from now on; one planned for later
    holds nothing now, so a new one is written. (None, False, why) when this host has no
    `ttp machines list` entry or the ledger refused."""
    from . import machine_ledger as ml
    from .runner import boot_id
    now = time.time() if now is None else now
    alias = this_host()
    if not alias:
        return None, False, ("this host has no entry in `ttp machines list`, so other projects here are not held "
                             "(`ttp machines add` it)")
    for c in _intents(alias):
        start, end = ml.cycle_window(c)
        if start <= now < end and not _rebooted_since(c):
            if c["at"] < now + wait_s:
                try:
                    c = ml.retime_power_cycle(c["id"], now + wait_s, p.name, now=now, hold_from=now, drain=now)
                except ValueError:
                    continue
            return c, False, f"planned host cycle already announced by {c.get('by')} (machine change #{c['id']})"
    task = os.environ.get("TTP_TASK")
    try:
        c = ml.add_power_cycle(alias, f"{max(1, math.ceil(wait_s))}s", p.name, reason,
                               int(task) if task and task.isdigit() else None, now=now,
                               extra={"boot": boot_id(), "reason": reason, "hold_from": now})
    except ValueError as e:
        return None, False, f"could not announce the planned host cycle: {e}"
    return c, True, (f"planned host cycle announced (machine change #{c['id']}): every project on this host "
                     "starts no new worker or reviewer runs, checks or pushes until it")


def before(c: dict, by: str, since: float) -> dict:
    """Intent c's time fields (machine_ledger.RETIMED) as they were before `by` first edited them at or
    after `since`: what a drain that took c up at `since` found."""
    from . import machine_ledger as ml
    cur = {k: c.get(k) for k in ml.RETIMED}
    e = next((e for e in c.get("edits") or [] if e.get("by") == by and float(e.get("at") or 0) >= since), None)
    return {**cur, **{k: v for k, v in (e or {}).get("was", {}).items() if k in ml.RETIMED}}


def cycle_now(p: Project, c: dict, now: float | None = None, since: float | None = None) -> None:
    """The drain recorded the cycle: intent c's time moves to now, so its hold and lapse count from the
    cycle itself, not from when the drain thought it might end. `since`: when that drain started."""
    from . import machine_ledger as ml
    try:
        ml.retime_power_cycle(c["id"], time.time() if now is None else now, p.name, now=now, drain=since)
    except ValueError:   # closed meanwhile
        pass


def release(p: Project) -> list[int]:
    """`ttp resume`: close the open intents for this host that p's own drain wrote on this boot but never
    recorded a cycle for (the drain was stopped), so the host is not held for a cycle that is not
    coming. One another project's drain took up (its edits) is left to it. The ids closed."""
    from . import machine_ledger as ml
    alias = this_host()
    if not alias:
        return []
    recorded = set()
    for d in _state_dirs(p):
        for f in (d / FOLDER).glob("*/record.json"):
            try:
                recorded.add(json.loads(f.read_text()).get("intent"))
            except (OSError, ValueError, AttributeError):
                continue
    out = []
    for c in _intents(alias):
        if c.get("by") == p.name and "boot" in c and c["id"] not in recorded and not _rebooted_since(c) \
                and all(e.get("by") == p.name for e in c.get("edits") or []):
            try:
                ml.close_change(c["id"], f"{p.name} resumed: its drain recorded no host cycle", by=p.name)
            except ValueError:
                continue
            out.append(c["id"])
    return out


def _no_reboot_records(p: Project | None, c: dict) -> list[tuple[str, dict]]:
    """(project, record) for the host-cycle records made with intent c that say the cycle did not happen
    (a dry run, or `rebooted` false), newest first. Each record carries its file's resolved `path`."""
    out = []
    for owner, d in _named_state_dirs(p):
        try:
            files = list((d / FOLDER).glob("*/record.json"))
        except OSError:
            continue
        for f in files:
            try:
                r = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(r, dict) and r.get("intent") == c.get("id") and (r.get("dry_run") or r.get("rebooted") is False):
                out.append((owner, {**r, "path": str(f.resolve())}))
    return sorted(out, key=lambda x: float(x[1].get("started") or 0), reverse=True)


def _no_reboot(p: Project | None, c: dict, records: list[tuple[str, dict]] | None = None) -> bool:
    """Whether a record made on intent c's boot by the drain that wrote intent c says the cycle did not
    happen. A drain that only took c up (its record has `intent_was`) does not close it: the planned cycle
    of the one that wrote it is still coming."""
    return any(r.get("boot_id") == c.get("boot") and not isinstance(r.get("intent_was"), dict)
               for _, r in (_no_reboot_records(p, c) if records is None else records))


def _undone(c: dict, records: list[tuple[str, dict]]) -> tuple[dict, str] | None:
    """Intent c's time fields (machine_ledger.RETIMED) from before the retimes made since its last undo,
    newest first back through its edit trail, and the mark of that undo (the newest such record's resolved
    path), when every one of those retimes was made by a drain that took c up on this boot and then
    recorded no reboot (the edit's `drain` is that record's `since`); else None: nothing to undo, or a retime
    stands for a drain still waiting (or for a cycle that is coming), so nothing is put back yet."""
    from . import machine_ledger as ml
    from .runner import same_boot
    takeups = [(str(r.get("project") or owner), r) for owner, r in records
               if isinstance(r.get("intent_was"), dict) and same_boot(r.get("boot_id"))]
    edits = list(c.get("edits") or [])
    last = max((i for i, e in enumerate(edits) if isinstance(e, dict) and e.get("undo")), default=-1)
    pending = [e for e in edits[last + 1:] if isinstance(e, dict)]
    if not pending or not takeups:
        return None
    newest = None
    for e in pending:
        by = next(((n, r) for n, r in takeups if e.get("by") == n and (
            e["drain"] == r["since"] if e.get("drain") is not None and r.get("since") is not None
            else float(e.get("at") or 0) >= float(r.get("since") or 0))), None)
        if by is None:
            return None
        newest = by[1] if newest is None or float(by[1].get("started") or 0) > float(newest.get("started") or 0) \
            else newest
    was = {k: c.get(k) for k in ml.RETIMED}
    for e in reversed(pending):
        was.update({k: v for k, v in (e.get("was") or {}).items() if k in ml.RETIMED})
    return was, f"{newest.get('path') or newest.get('id')}"


def _undo_takeups(p: Project | None, c: dict, records: list[tuple[str, dict]], settle: bool, now: float) -> dict:
    """Intent c with the retimes of the drains that took it up and then recorded no reboot undone (_undone),
    so a planned cycle a drain only took up keeps its own time and window. With `settle` the ledger is
    written (once per mark, and only if no edit came meanwhile); else only the returned copy."""
    from . import machine_ledger as ml
    hit = _undone(c, records)
    if hit is None:
        return c
    was, mark = hit
    if not settle:
        return {k: v for k, v in c.items() if k not in ml.RETIMED} | {k: v for k, v in was.items() if v is not None}
    try:
        return ml.restore_power_cycle(c["id"], was, p.name if p else "", mark, now=now,
                                      edits=len(c.get("edits") or []))
    except ValueError:   # closed meanwhile
        return c


def hold(p: Project | None, now: float | None = None, settle: bool = False) -> dict | None:
    """The power-cycle intent that holds new runs on this host now, or None: open, due (its
    machine_ledger.cycle_window holds now) and made on this boot. With `settle`
    (the daemon), an intent whose record says the cycle did not happen is closed. One that a drain took up
    (its record has `intent_was`) is not its own: that drain's retime is undone instead (_undo_takeups),
    so it holds only when its own window does."""
    from . import machine_ledger as ml
    now = time.time() if now is None else now
    alias = this_host()
    if not alias:
        return None
    for c in _intents(alias):
        if _rebooted_since(c):
            continue
        records = _no_reboot_records(p, c)
        c = _undo_takeups(p, c, records, settle, now)
        start, end = ml.cycle_window(c)
        if not start <= now < end:
            continue
        if _no_reboot(p, c, records):
            if settle:
                try:
                    ml.close_change(c["id"], "the planned host cycle did not happen: its record says no reboot",
                                    by=p.name if p else "", now=now)
                except ValueError:
                    pass
            continue
        return c
    return None


def lapsed(now: float | None = None) -> list[dict]:
    """Open intents for this host whose hold ran out (POWER_CYCLE_LEAD_S past their time) with no
    new boot: the cycle did not come in time."""
    from . import machine_ledger as ml
    now = time.time() if now is None else now
    alias = this_host()
    return [c for c in _intents(alias) if c["at"] + ml.POWER_CYCLE_LEAD_S <= now and not _rebooted_since(c)] \
        if alias else []


def reason(c: dict) -> str:
    what = str(c.get("what") or "")
    return str(c.get("reason") or (what.split(": ", 1)[1] if ": " in what else ""))


def hold_line(c: dict, where, now: float | None = None) -> str:
    """'holding new runs: planned host cycle by <project> at HH:MM <zone> (<reason>)'."""
    from . import timefmt
    why = reason(c)
    return (f"holding new runs: planned host cycle by {c.get('by') or 'unknown'} at "
            f"{timefmt.short(float(c['at']), where, now)}" + (f" ({why})" if why else ""))


def host_runs(p: Project) -> list[tuple[str, dict]]:
    """(project, run) for the runs and pushes (role 'push', the task in 'pushing') in flight in the
    other projects on this host, read only; a project whose database cannot be read is skipped."""
    from .globalcap import connect_ro, local_projects
    out = []
    for name, db in local_projects(skip=str((p.state / "project.db").resolve())):
        try:
            conn = connect_ro(db)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute("SELECT id,task,role FROM runs WHERE status='running' ORDER BY id")]
                rows += [{"id": None, "task": t["id"], "role": "push"}
                         for t in conn.execute("SELECT id FROM tasks WHERE status='pushing' AND COALESCE(result,'') NOT LIKE '%\"push_verify\"%' ORDER BY id")]
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        out += [(name, r) for r in rows]
    return out
