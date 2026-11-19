# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Idle capacity, model-free (Daemon.check_idle_capacity): a device the project holds sits unused.

A device resource of the project (candidates) is idle while no open task names it, no `ttp lock` slot
of it is taken or reserved and no detached-job hold keeps it. Paused resources (the user's, the
project's own or a shared one), resources in trouble and machines with an open ledger condition are
never candidates, nor is a machine an open change ledger entry holds (holds_work: its broker or
queue held, paused, drained or its device work disabled, said as a phrase; file names and
recovery switches do not count), nor a device no queued, running or waiting
(deferred included) task names and no task run in the last `coordinator.idle_capacity_use_days` (7;
0: no such filter) named: the project does not use it. Once one has been idle for `coordinator.idle_capacity_s` (1500 s; 0: off) while a
worker slot is free, the budget gate is green or yellow and dispatch would start nothing queued, one
normal `idle_capacity` event asks the coordinator to fill it with charter work or say once that
nothing useful remains.

Debounce (step): one event per idle stretch. A stretch ends when the device is used, paused or in
trouble again; the next one counts its grace afresh. A stretch still idle after its event raises again
only after `coordinator.idle_capacity_rearm_s` (6 h), doubling each time up to REARM_MAX_S, so a
project with nothing for its device costs a few turns a day at most. The state is kept in the
database, so a restart never raises a stretch twice.

Declines (judge): once the coordinator handled an event, a resource no task created since names was
declined. Its fire count and time are kept per resource (DECLINED_KEY, in the database), so a new
stretch of it (after a pause or a trouble spell) waits both its grace and the doubling rearm from
its last event, never less than an undeclined one. An open task naming the resource, or a task created after an event naming it, clears that.
"""
from __future__ import annotations

import json
import re

from . import locks
from .machines import retired

KIND = "idle_capacity"
STATE_KEY = "idle_capacity"   # kv: {resource: {"since", "fired", "n"}} of the open idle stretches
GRACE_S = 1500.0
REARM_S = 6 * 3600.0
REARM_MAX_S = 48 * 3600.0
SUFFIX = "-device"
LABEL_LOOKBACK_S = 14 * 86400   # task labels this recent name the project's device resources
USE_DAYS = 7.0
DECLINED_KEY = "idle_capacity_declined"   # kv: {resource: {"n", "at"}} the coordinator answered with no task
_OBJ = (r"(?:(?:the|its|all|every|new|this|that|a)\s+)*(?:[\w-]+\s+)?"
        r"(?:broker|queue|device\s+jobs?|device\s+work|jobs|box|machine|node|host|devices?|dispatch|scheduler)\b"
        r"(?!\s+(?:from\s+)?(?:auto\w*|resets?|resetting|re-?power\w*|reboot\w*|power[\s-]?cycl\w*|recovery|watchdog"
        r"|restart\w*)\b)")
# Holding device work takes the verb applied to the broker, queue, jobs or box as a phrase: a bare
# 'hold' or 'stops' elsewhere (a file name, 'the broker stops the keeper') and recovery switches
# (auto reset, power cycle, reboot) do not count.
_HOLD = re.compile(
    rf"\b(?:hold|holds|holding|held|paus\w*|drain\w*|stop|stopped|stopping|block|blocked|blocking|disabl\w*"
    rf"|quiesc\w*|cordon\w*|fenc\w*|reserv\w*)\s+(?:on\s+)?{_OBJ}"
    rf"|\b{_OBJ}\s+(?:(?:is|was|are|were|kept|stays|left|now|set|taken)\s+)*"
    rf"(?:held|paused|drained|stopped|disabled|blocked|quiesced|cordoned|fenced|reserved|offline|down|on\s+hold"
    rf"|out\s+of\s+service|in\s+maintenance)\b"
    r"|\bout\s+of\s+service\b|\bno\s+(?:new\s+)?device\s+(?:jobs?|work)\b", re.I)
_TOKEN = re.compile(r"\S*[./_=]\S*")   # file names, paths, env settings and a/b lists: no phrase there


def settings(cfg: dict) -> tuple[float, float]:
    """(grace_s, rearm_s) from the `coordinator` config; grace 0 turns the check off."""
    c = cfg.get("coordinator") or {}
    grace, rearm = c.get("idle_capacity_s", GRACE_S), c.get("idle_capacity_rearm_s", REARM_S)
    return float(GRACE_S if grace is None else grace), float(REARM_S if rearm is None else rearm)


def use_days(cfg: dict) -> float:
    """`coordinator.idle_capacity_use_days`: a device no task named this recently is not the project's."""
    v = (cfg.get("coordinator") or {}).get("idle_capacity_use_days", USE_DAYS)
    return float(USE_DAYS if v is None else v)


def holds_work(change: dict) -> bool:
    """Whether an open change ledger entry holds or disables device work on its machine."""
    return bool(_HOLD.search(_TOKEN.sub(" ", str(change.get("what") or ""))))


def judge(declined: dict, fired: dict, named: set[str], at: float) -> dict:
    """The declines after the coordinator handled one event raised at `at` for `fired` ({resource: fire
    count}); `named`: what the tasks created since name. A named resource is cleared, the rest declined."""
    out = dict(declined or {})
    for res, n in (fired or {}).items():
        if any(uses(x, res) for x in named):
            out.pop(res, None)
        else:
            out[res] = {"n": max(int(n or 0), int((out.get(res) or {}).get("n") or 0)), "at": float(at)}
    return out


def labels(task: dict) -> list[str]:
    try:
        v = json.loads(task.get("labels") or "[]")
    except ValueError:
        return []
    return [lb for lb in v if isinstance(lb, str)] if isinstance(v, list) else []


def task_names(task: dict, cfg: dict) -> set[str]:
    """The resources a task names (`resource:`, `exclusive:`), device-lock aliases as their lock;
    `needs_device` as the configured device lock."""
    out = {locks.canonical(cfg, lb.split(":", 1)[1]) for lb in labels(task)
           if lb.split(":", 1)[0] in ("resource", "exclusive") and ":" in lb}
    dev = (cfg.get("device") or {}).get("locks") or []
    if dev and "needs_device" in labels(task):
        out.add(str(dev[0]))
    return out - {""}


def uses(name: str, resource: str) -> bool:
    """Whether a task or lock naming `name` uses device `resource`: the same name, or a machine alias
    and its `<alias>-device` resource."""
    return name == resource or name == resource + SUFFIX or resource == name + SUFFIX


def candidates(cfg: dict, named: set[str], resource_machines: list[str], machines: dict[str, dict]) -> set[str]:
    """The project's device resources: its configured device lock, every `*-device` name its config or
    recent tasks name, and each machine its charter's Resources names that is tagged `device`. A machine
    is dropped when its `<alias>-device` resource is there too: they are one device. A retired machine's
    resource (machines.retired) is never one."""
    out = set()
    dev = (cfg.get("device") or {}).get("locks") or []
    if dev:
        out.add(str(dev[0]))
    shared = cfg.get("shared_resources") or []
    for name in {*named, *(cfg.get("resources") or {}), *(x for x in shared if isinstance(x, str))}:
        if str(name).endswith(SUFFIX):
            out.add(locks.canonical(cfg, str(name)))
    out |= {a for a in resource_machines if "device" in ((machines.get(a) or {}).get("tags") or [])}
    gone = {a for a, m in machines.items() if retired(m)}
    return {n for n in out if n and n + SUFFIX not in out and n not in gone
            and not (n.endswith(SUFFIX) and n[:-len(SUFFIX)] in gone)}


def step(st: dict | None, idle: set[str], now: float, grace: float, rearm: float,
         may_fire: bool, declined: dict | None = None) -> tuple[dict, list[str]]:
    """The idle stretches after one check, and the resources to raise now.

    `idle`: the device resources idle right now. One that is not ends its stretch. `may_fire`: a slot
    is free, the gate allows new work and nothing queued would start; when false no event is raised,
    but the stretches go on (a busy slot does not make the device any less idle). `declined`: a new
    stretch of a declined resource starts where its last event left the rearm."""
    old, declined = st or {}, declined or {}
    new, fired = {}, []
    for name in sorted(idle):
        dec = declined.get(name) or {}
        rec = dict(old.get(name) or ({"since": now, "fired": float(dec["at"]), "n": int(dec.get("n") or 1)}
                                     if dec.get("at") else {"since": now}))
        if may_fire and grace > 0:
            n = int(rec.get("n") or 0)
            # Never before the grace of this stretch, nor before the rearm after the last event: a
            # declined resource's new stretch waits at least as long as an undeclined one.
            due = float(rec["since"]) + grace
            if rec.get("fired"):
                due = max(due, float(rec["fired"]) + min(rearm * 2 ** max(n - 1, 0), REARM_MAX_S))
            if (n == 0 or rearm > 0) and now >= due:
                rec.update(fired=now, n=n + 1)
                fired.append(name)
        new[name] = rec
    return new, fired


def text(stretches: dict, fired: list[str], now: float, slots: int, running: list[str], next_check: str,
         rearm: float) -> str:
    """The event: which devices sit idle and since when, what runs, the next check, what to do."""
    idle = "; ".join(f"{n} for {(now - float(stretches[n]['since'])) / 60:.0f} min" for n in fired)
    again = (f"again only after it is used or {rearm / 3600:g} h from now (longer each time)" if rearm > 0
             else "again only after it is used")
    return (f"Idle capacity: device resource(s) idle with nothing queued that uses them: {idle}. "
            f"{slots} worker slot(s) free. Running: {'; '.join(running) or 'nothing'}. "
            f"Next scheduled check: {next_check or 'none'}. Queue useful charter work for it, or say "
            f"once that nothing useful remains (no ask). This is raised {again}.")
