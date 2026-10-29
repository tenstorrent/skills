# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-free heal checks: a declared health check, a safe fix, and escalation.

A command schedule may carry a `heal` block in its payload (harness/schedules.json or schedule_set):

    "heal": {"check": "<probe>", "fix": "<safe command>", "suppress": "<probe>", "resource": "<lock>", "grace_s": 300,
             "settle_s": 60, "max_fixes": 3, "window_h": 1, "timeout_s": 120, "unknown_cap_s": 3600}

`check` exits 0 healthy, 1 unhealthy, 75 or 255 unknown (the codes of start_when); any other exit,
or a timeout, is unknown too and reported as an error of the schedule. Each run of the schedule:
- healthy: the check's state clears (and an open outage alert with it);
- unknown: nothing changes and nothing is fixed. A check without `outage` that stays unknown for
  `unknown_cap_s` (0: never) without a break says so once, through escalate_to(): "user" gets one normal
  alert `heal-unknown:<name>`, "coordinator" one high observation (`heal:<name>:unknown:<since>`), each
  with the last check output and the blocker (exit code and last output line). The next healthy or
  unhealthy result ends that stretch, so a later one can say so again;
- unhealthy within `grace_s` of the first unhealthy run: tolerated (automatic recovery may still
  happen); the check runs again when the grace ends;
- unhealthy past grace: the fix runs (under `ttp lock <resource>` when set; a busy or paused lock
  defers it uncounted, but a lock that stays busy, not paused, past `window_h` escalates as below; a fix
  that has waited on a pause for `unknown_cap_s` says so once, naming the pause, who set it and that lifting
  it lets the fix run: a pause the user set (or of unknown origin) gets one low feed line and no ask,
  any other one high coordinator observation (`heal:<name>:paused:<since>`); never a lift of the pause;
  a fix that itself exits 75 counts like any other) and the check runs again `settle_s` later. Healthy then: one low feed line and a digest
  record ("fixed X, n today"), no coordinator wake.
- deliberately down: a block with `suppress` (a read-only probe) runs it first, before the fix, the
  cap or the escalation below, for at most SUPPRESS_S. Exit 0 says the resource is down on purpose
  (a planned shutdown): nothing is fixed or escalated and the daemon log says "suppressed: deliberately
  down"; the episode stays open, so once the probe exits non-zero the fix runs on the next run. Exit 1
  heals normally; any other exit or a timeout (a broken probe) heals normally too and is logged.
- the fix failed, did not help, `max_fixes` per `window_h` is used up, or there is no fix: one
  priority-1 self-fix task is queued (label `heal:<name>`, so one is open per check), carrying the
  check's and the fix's output. Only once that task fails, blocks, or ends while the check still
  fails does a keyed high alert `heal:<name>` go out; it clears itself once the check is healthy.
  `escalate` picks who gets that last step: "user" (default; the project's `heal.escalate` config
  sets it for every check) sends the alert; "coordinator" sends no alert and instead queues one high
  observation for the coordinator per unhealthy episode (fingerprint `heal:<name>:<since>`).
  A different finding later in the episode (normalized output, so counters and times do not count) is
  told once more the same way (fingerprint `heal:<name>:<since>:<finding>`), up to FINDINGS_MAX per
  episode; a finding told before stays quiet. `suppress` exiting 0 tells no one here either. Moving escalation to the coordinator closes an earlier user alert with its reason. Adding a
  known fault that covers the current confirmed failure does the same, without claiming recovery.

An outage check whose `resource` this project paused (its own pause or a shared one) is not fixed,
alerts nobody and reports nothing to the machine ledger while the pause lasts. It stays silent for
the whole pause when the user set it, or when the pause's reason names the check: that outage is known
here. Any other pause (this project's or another project's, for a different reason) keeps it silent
only for the project's `heal.paused_quiet_s` (6 h) of the outage under it; then the coordinator, never
the user, is asked once per stretch (outage start and pause) whether the pause is still right. Nothing
here lifts a pause; lifting it restores the normal handling above.

Known faults and outages: `known_fault` (why repair is not ours) keeps a single fault quiet: no fix,
no task, no alert. It is text, or an object {"why": ..., "match": "<regex>", "until": "<ISO time>"}
(no other keys). `match` must find the check's output (its last 1000 characters) for the failure to
count as known; one it does not find is handled normally. `until` (UTC unless it names a zone) is at
most KNOWN_FAULT_S (7 d) ahead; without it the entry ends KNOWN_FAULT_S after it first covered a
failure, recorded in the check's state (`known_key`, `known_since`; kept while the check is healthy, so
a flapping fault never renews itself). A changed entry starts anew. Once its end passes, the failure is
handled normally again and one low feed line and one low observation ask the coordinator to renew the
entry or drop it. A check with `outage: true` watches a whole box or resource; once it has served
nothing for 60 min (or past its grace_s, if shorter) it escalates as above whatever `known_fault`
says. Its check staying unknown (75, 255 or a broken check: an unreachable box looks like that) for as
long escalates the same way, without running the fix; the next healthy result clears it. When every
unknown run of that stretch was only its route failing (ssh exit 255, or output saying the connection
was refused or timed out, or the host or network unreachable: ROUTE_RE), the box may well be up: it
escalates as this project's own route problem, not an outage, and reports nothing to the machine ledger. Observation
mutes never reach these checks: their escalation does not go through the observation screen.

Telling a box that is off from our own route: `ping_host` (a host name or address) and `ping_peer` (another
host on the same network), set together and pinged from the daemon's machine. A run that exits 255 (ROUTE_RC,
ssh's own failure) then pings them: `ping_host` silent while `ping_peer` answers means the box itself is off or
rebooting: that run is an outage like any other unknown run, so the box is reported down past the window.
Everything else keeps the route rule above. Known limits: with no peer answering, a silent box is never called
down (our own route looks the same); a route failure with another exit code, ping unable to tell, or a network
that drops ping stays a route problem. Set them only for a box that answers ping when it is up.

Known machine outages: while the check's machine (machine_of) or its `resource` has a known outage in the
machine ledger (`ttp machines outage`, until its end), an unhealthy result is kept quiet like a known
fault (no fix, no task, no alert), an outage check's unknown stretch never escalates, and a long unknown
stretch says nothing (no unknown_cap_s message); the status reads "known outage until <time>". One
without `covers` quiets every result. One with `covers` (`--covers`) quiets only the results it explains
(outage_explains), by one rule: a covered word (a plural too) in the check's identity, its name,
`resource` or `unit`, never in its output; or, with host, power or network among them, an unknown run
(75 or 255) without a FINDING line, which cannot reach the box. Any other result is handled normally, in
the episode it is in: a chip loss a health check reports under an outage covering one service escalates,
even when its output also names that service. Accepted gap: a check whose identity names a covered part
stays quiet under that outage, even when it also fails for another reason. Once the outage ends, a check
still failing starts a fresh episode.

Fresh episodes: an unhealthy result after an unknown stretch (ending in 75 or 255, not a broken check)
longer than its grace (at least FRESH_UNKNOWN_S) whose self-fix task is no longer open starts a new
episode, grace included. It keeps the fix history, so `max_fixes` per `window_h` still holds, and an
open self-fix task keeps the episode going. Known gap: a host reboot does not start a fresh episode;
the fix cap and any open self-fix task carry across reboots.

Untenanted holds: a check that prints a line `ttp-heal: untenanted-hold` (UNTENANTED; the broker preset
prints it while `hold_field` is set and its optional `holder_field` is empty) says a device is held with
no tenant, so queued work cannot use it. Seen on every run (any exit code, 75 included) for the project's
`heal.untenanted_hold_s` (UNTENANTED_HOLD_S), it pages once per stretch, whatever else keeps the check
quiet: a known machine outage, a check standing down with 75, a known fault or an observation mute. The
page is a keyed high alert `heal-hold:<name>` to the user, or with `escalate` "coordinator" one high
observation. Only a pause of the check's `resource` that the user set, or whose reason names the check,
keeps it quiet. Its age is the episode's: the stretch ends only once runs answer (0 or 1) without the line
for HOLD_CLEAR_S (1 h), so a loop of failing automatic resets that frees the device between two runs never
restarts it. The alert lives like the check's `heal:<name>` one (cleared_why with `hold`): the sweep
clears it once that stretch ends, the check loses its enabled schedule or heal
block, escalation moves to the coordinator or such a pause covers it. An outage keeps silencing
everything else the check reports.

Non-outage findings: a check that exits non-zero and prints a line `ttp-heal: not-outage` (FINDING) says
the box or resource is up and something narrower is wrong. That run counts as plain unhealthy (exit 1)
on a check without `outage`: its own grace, its fix, `known_fault` keeps it quiet, the ordinary self-fix
task, no outage wording, and nothing reported to the machine ledger (an earlier down report clears).
A check that never prints the line keeps the behaviour above.

Recovery switched off: a check that prints a line `ttp-heal: recovery-off` (RECOVERY_OFF; the broker preset
prints it while an `auto_fields` flag is off or its optional `recovery_off_field` reports a disabled
recovery step) counts as unhealthy (exit 1) whatever its exit code, and no exemption covers it: not a
known, owned state below, a known machine outage, nor a `known_fault`. It runs the normal grace, fix,
task and escalation, so an override that switches recovery off always surfaces, whoever owns the machine.

Known, owned states: a check that exits 75 or 1 and prints a line `ttp-heal: known <owner>: <why>` (KNOWN_RE)
says it knows what is going on and who owns it (a pause it reads, a machine outage, a fault handed to a
recovery owner). That run is not unknown and not unhealthy: no unknown_cap_s message, no outage window, no
fix, no task; it ends the current unknown stretch, and the status reads "known (<owner>: <why>)". Each run
says so for itself: the next run without the line starts or keeps the ordinary unknown stretch, or is
handled as healthy or unhealthy as above.

Backing off while healthy: `healthy_every` (an interval like the schedule's `every`, e.g. "1h", at least
`every`) runs the check that often once it was healthy `healthy_runs` (HEALTHY_RUNS, 3) runs in a row. The
first run that is anything else (unhealthy, unknown, a known state, an error or a fix that ran, and a run
that timed out, crashed or whose own command failed) goes straight back to `every`; a run the daemon
recorded failed at its timeout never extends the streak, even when it ends healthy later. The streak lives in the check's state (`healthy_streak`); `ttp heal list` and `ttp schedules`
show the interval in force.

Daemon time: `timeout_s` is capped at MAX_TIMEOUT_S (the daemon's command-watcher cap), a timeout
kills the check's or fix's whole process group, and the daemon tells its watchdog it still moves
between the check and the fix; after a fix the schedule's own command waits for the recheck, so one
step never outlasts the watchdog.

Presets fill in `check` (and a default `fix`) from config, so no host or unit is named in code:
- `{"preset": "systemd", "unit": "x.service", "user": false, "host": null}`: active and not
  crash-looping (NRestarts rising since the last check); fix `systemctl restart` (over ssh to `host`).
- `{"preset": "http", "url": "...", "expect": [200]}`: the URL answers with an expected status
  (any 2xx/3xx by default). No default fix.
- `{"preset": "broker", "status_command": "...", "hold_field": "held", "auto_fields": [...]}`: the
  command prints JSON; unhealthy while `hold_field` is true or any of `auto_fields` (automatic
  recovery: power cycle, reboot) is false. Optional `expected_count` requires JSON `count` to match;
  `max_recent_failures` bounds JSON `recent_failures`. Missing or invalid fields are unknown.
  No default fix.

Shared machines: a check that watches one of the user's machines (`machine: "<alias>"`, or a
`resource` naming a known alias on an `outage` or broker check) reports what it sees to the machine
ledger (machine_ledger.report), so the machine's recovery owner is paged once the condition outlasts
the check's grace: `held` while a broker's hold field is set, otherwise `down` (`condition` overrides
it); an unknown result counts only once an outage check escalates on it, and never when only its route
failed (above); a single known fault never does. An outage check's report carries its `known_fault`,
which keeps the page from escalating past the owner (machine_ledger: Quiet outages). The next healthy
result reports it cleared.

Long fixes: `fix_detached: true` runs the fix as a detached job (detach.start) the daemon tracks, for at
most `fix_timeout_s` (FIX_TIMEOUT_S, capped at FIX_MAX_TIMEOUT_S) instead of `timeout_s`: a redeploy that
takes minutes. Its job lives under state/heal-fix/<check>/ (fix.rc, fix.log). The job is `ttp lock <resource>`
around the fix (as above), so the lock is held for the fix's whole run and released when it ends: the fix
must run in the foreground, never detach itself. While it runs, each run of the schedule skips the check
(check() reads 75, busy, for `ttp heal test` too) and comes back every FIX_POLL_S; a daemon restart keeps
it. Its end is a fix's end: exit 0 settles and rechecks, the lock's 75 defers it, any other exit escalates.
Past `fix_timeout_s` the daemon kills its process group (exit 124); a job gone without an exit code (a host
reboot) is a failed fix with the reason.

State lives in the database (kv `heal:<name>`), so a daemon restart, even in the middle of a fix,
picks up where it was: a fix that started counts toward the cap, and the next run is its recheck,
no earlier than `settle_s` after the fix started.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import outagemsg, pageto, schedrun, screen, scriptwatch, timefmt
from .db import DB
from .project import command_env, durable_write

STATE_KEY = "heal:"             # kv per check: its state (see run)
FIXED_KEY = "heal_fixed"        # kv: [{"at", "name"}] successful fixes, last 7 days
LABEL = "heal:"                 # task label of a check's self-fix task
UNKNOWN_RCS = (75, 255)
OUTAGE_S = 3600                 # a whole box or resource serving nothing this long is never kept quiet
PAUSED_QUIET_S = 21600          # heal.paused_quiet_s default: an outage under a pause it is not known by
KNOWN_FAULT_S = 7 * 86400       # a known fault's longest life: `until` at most this far ahead, else this long
LOCK_WAIT_S = 30                # how long a fix waits for its resource lock before it is deferred
SUPPRESS_S = 30                 # a `suppress` probe's longest run (or timeout_s, if shorter); then it heals
MAX_TIMEOUT_S = 240             # timeout_s cap: daemon.WATCHER_MAX_S, so check, then fix (+ lock wait) stay
                                # each well below the daemon's watchdog (WATCHDOG_S)
FINDING = "ttp-heal: not-outage"   # a check output line: unhealthy, but the box is up (module doc)
KNOWN_RE = re.compile(r"ttp-heal: known ([^:\s][^:]*?): *(\S.*)")   # a check output line: a known, owned state
UNTENANTED = "ttp-heal: untenanted-hold"   # a check output line: a device held with no tenant (module doc)
UNTENANTED_HOLD_S = 14400       # heal.untenanted_hold_s default: an untenanted hold pages after this long
HOLD_KEY = "heal-hold:"         # alert key of an untenanted hold's page
HOLD_CLEAR_S = 3600             # an untenanted hold's stretch ends only once runs answer without it this long
RECOVERY_OFF = "ttp-heal: recovery-off"   # a check output line: automatic recovery is switched off (module doc)
# An unknown check result that is only its route failing (module doc): ssh's own exit code, or its words.
ROUTE_RC = 255
ROUTE_RE = re.compile(r"connection (refused|timed out|reset)|operation timed out|no route to host|"
                      r"(host|network) is unreachable|could not resolve host(name)?|name or service not known|"
                      r"temporary failure in name resolution|kex_exchange_identification|"
                      r"ssh: connect to host|connection closed by .* port", re.I)
HOST_RE = re.compile(r"[A-Za-z0-9_.:%\[\]][A-Za-z0-9_.:%\[\]-]*")   # one host name or address, never an option
PING_S = 3                      # how long box_silent waits for each ping reply
FINDINGS_MAX = 5               # distinct findings told per escalated episode (_escalated); later ones stay quiet
FRESH_UNKNOWN_S = 900          # an unknown stretch shorter than this never starts a fresh episode (module doc)
FIX_TIMEOUT_S = 1800            # fix_timeout_s default: a detached fix's longest run (module doc)
FIX_MAX_TIMEOUT_S = 14400       # fix_timeout_s cap
FIX_POLL_S = 60                 # a detached fix's end is looked for this often
FIX_DIR = "heal-fix"            # state/heal-fix/<check>/: a detached fix's job
FIX_OWN_75 = 176                # a fix under a lock that itself exits 75 exits this, apart from the lock's 75
NOTE_MAX = 2000                 # self_fix_note cap: a longer note is cut to this
HEALTHY_RUNS = 3                # healthy_runs default: healthy runs in a row before `healthy_every` applies
STREAK_KEY = "healthy_streak"   # a check's state: its plain healthy runs in a row
DEFAULTS = {"grace_s": 300, "settle_s": 60, "max_fixes": 3, "window_h": 1.0, "timeout_s": 120, "unknown_cap_s": 3600}
OPEN = ("queued", "running", "waiting", "needs_review")
# Every key a block takes, with its JSON type. validate() accepts exactly these keys, and the coordinator's
# structured-output schema (coordinator.HEAL_SCHEMA) is built from these types, so a new key reaches both.
_TEXT, _FLAG, _COUNT = {"type": "string"}, {"type": "boolean"}, {"type": "integer"}
KNOWN_FAULT_KEYS = ("why", "match", "until")   # known_fault as an object (module doc)
COMMON_FIELDS: dict[str, dict] = {
    **dict.fromkeys(("check", "fix", "suppress", "resource", "machine", "condition", "preset", "escalate",
                     "ping_host", "ping_peer", "to", "self_fix_note"), _TEXT),
    "known_fault": {"anyOf": [_TEXT, {"type": "object", "properties": dict.fromkeys(KNOWN_FAULT_KEYS, _TEXT),
                                      "required": ["why"]}]},
    "healthy_every": {"anyOf": [_TEXT, _COUNT]}, "healthy_runs": _COUNT,
    "outage": _FLAG, "fix_detached": _FLAG, "fix_timeout_s": {"type": "number"},
    **dict.fromkeys(DEFAULTS, {"type": "number"})}
PRESET_FIELDS: dict[str, dict[str, dict]] = {
    "systemd": {"unit": _TEXT, "user": _FLAG, "host": _TEXT},
    "http": {"url": _TEXT, "expect": {"type": "array", "items": {"type": "integer"}}},
    "broker": {"status_command": _TEXT, "hold_field": _TEXT, "holder_field": _TEXT, "auto_fields": {"type": "array", "items": _TEXT},
               "recovery_off_field": _TEXT,
               "expected_count": _COUNT, "max_recent_failures": _COUNT},
}
_COMMON = set(COMMON_FIELDS)
ESCALATE = ("user", "coordinator")   # who hears of a check its self-fix task could not repair
PRESETS: dict[str, set[str]] = {name: set(fields) for name, fields in PRESET_FIELDS.items()}
BROKER_HOLD = "held"
BROKER_AUTO = ("auto_power_cycle", "auto_reboot")
LEDGER_CONDITIONS = ("down", "held")   # machine_ledger.CONDITIONS


# The block ----------------------------------------------------------------------------------------
def validate(block: Any, where: str = "heal", every_s: int | None = None) -> dict:
    """The heal block checked and with its defaults filled in. Raises ValueError saying what is wrong.
    `every_s`, the schedule's period when known, is the floor of `healthy_every`."""
    if not isinstance(block, dict):
        raise ValueError(f"{where} is an object")
    preset = block.get("preset")
    if preset is not None and preset not in PRESETS:
        raise ValueError(f"{where}: `preset` must be one of {', '.join(PRESETS)}")
    allowed = _COMMON | PRESETS.get(preset, set())
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}; use {', '.join(sorted(allowed))}")
    out = {**DEFAULTS, **block}
    for k in DEFAULTS:
        v = out[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{where}: `{k}` is a number, 0 or more")
    if out["timeout_s"] <= 0 or out["window_h"] <= 0:
        raise ValueError(f"{where}: `timeout_s` and `window_h` must be positive")
    if out["timeout_s"] > MAX_TIMEOUT_S:   # longer would hold the daemon's tick past its watchdog
        # schedule_set refuses it (schedule.over_caps); a stored one runs at the cap, its timeout text says so
        out["timeout_asked_s"], out["timeout_s"] = out["timeout_s"], MAX_TIMEOUT_S
    for k in ("check", "fix", "suppress", "resource", "machine", "self_fix_note"):
        if out.get(k) is not None and not isinstance(out[k], str):
            raise ValueError(f"{where}: `{k}` is text")
    if out.get("self_fix_note") is not None:
        out["self_fix_note"] = out["self_fix_note"].strip()[:NOTE_MAX]
    if out.get("known_fault") is not None:
        _validate_known(out["known_fault"], where)
    if out.get("to") is not None and (not isinstance(out["to"], str) or not out["to"].strip()):
        raise ValueError(f"{where}: `to` is a project name or {pageto.OWNER}")
    if not isinstance(out.get("outage", False), bool):
        raise ValueError(f"{where}: `outage` is true or false")
    if not isinstance(out.get("fix_detached", False), bool):
        raise ValueError(f"{where}: `fix_detached` is true or false")
    if out.get("fix_detached"):
        v = out.setdefault("fix_timeout_s", FIX_TIMEOUT_S)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ValueError(f"{where}: `fix_timeout_s` is a number of seconds, above 0")
        if v > FIX_MAX_TIMEOUT_S:   # as timeout_s above
            out["fix_timeout_asked_s"], out["fix_timeout_s"] = v, FIX_MAX_TIMEOUT_S
    elif "fix_timeout_s" in out:
        raise ValueError(f"{where}: `fix_timeout_s` is for a detached fix: set `fix_detached` true too")
    if out.get("healthy_every") is not None:
        v = healthy_every_s(out)
        if v <= 0:
            raise ValueError(f"{where}: `healthy_every` is an interval (e.g. 1h, 30m or seconds), above 0")
        if every_s is not None and v < every_s:
            raise ValueError(f"{where}: `healthy_every` ({out['healthy_every']}) must be at least the schedule's "
                             f"`every` ({every_s} s)")
        n = out.setdefault("healthy_runs", HEALTHY_RUNS)
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError(f"{where}: `healthy_runs` is an integer, 1 or more")
    elif "healthy_runs" in out:
        raise ValueError(f"{where}: `healthy_runs` goes with `healthy_every`: set that too")
    for k in ("ping_host", "ping_peer"):
        v = out.get(k)
        if v is not None and not (isinstance(v, str) and HOST_RE.fullmatch(v)):
            raise ValueError(f"{where}: `{k}` is one host name or address")
    if bool(out.get("ping_host")) != bool(out.get("ping_peer")):
        raise ValueError(f"{where}: `ping_host` and `ping_peer` are set together")
    if out.get("escalate") is not None and out["escalate"] not in ESCALATE:
        raise ValueError(f"{where}: `escalate` is one of {', '.join(ESCALATE)}")
    if out.get("condition") is not None and out["condition"] not in LEDGER_CONDITIONS:
        raise ValueError(f"{where}: `condition` is one of {', '.join(LEDGER_CONDITIONS)}")
    if preset == "systemd":
        if not str(out.get("unit") or "").strip():
            raise ValueError(f"{where}: preset systemd needs `unit`")
        if not isinstance(out.get("user", False), bool):
            raise ValueError(f"{where}: `user` is true or false")
        if out.get("fix") is None:
            out["fix"] = _systemctl(out, "restart")
    elif preset == "http":
        if not str(out.get("url") or "").startswith(("http://", "https://")):
            raise ValueError(f"{where}: preset http needs `url` (http:// or https://)")
        exp = out.get("expect")
        if exp is not None and not (isinstance(exp, list) and all(isinstance(c, int) for c in exp)):
            raise ValueError(f"{where}: `expect` is a list of HTTP status codes")
    elif preset == "broker":
        if not str(out.get("status_command") or "").strip():
            raise ValueError(f"{where}: preset broker needs `status_command`, a command printing JSON")
        auto = out.get("auto_fields")
        if auto is not None and not (isinstance(auto, list) and all(isinstance(f, str) for f in auto)):
            raise ValueError(f"{where}: `auto_fields` is a list of field names")
        for k in ("expected_count", "max_recent_failures"):
            if k in out and (isinstance(out[k], bool) or not isinstance(out[k], int) or out[k] < 0):
                raise ValueError(f"{where}: `{k}` is an integer, 0 or more")
    elif not str(out.get("check") or "").strip():
        raise ValueError(f"{where} needs `check` (a shell probe: 0 healthy, 1 unhealthy, 75/255 unknown) "
                         f"or a `preset`")
    return out


def healthy_every_s(spec: dict) -> int:
    """`healthy_every` in seconds; 0 when unset or not an interval."""
    from .schedule import parse_every
    v = spec.get("healthy_every")
    try:
        return 0 if v is None or isinstance(v, bool) else parse_every(v)
    except (ValueError, TypeError):
        return 0


def _validate_known(kf: Any, where: str) -> None:
    """`known_fault`: text, or an object {why, match?, until?} (module doc)."""
    if isinstance(kf, str):
        return
    if not isinstance(kf, dict):
        raise ValueError(f"{where}: `known_fault` is text or an object {{why, match, until}}")
    unknown = sorted(set(kf) - set(KNOWN_FAULT_KEYS))
    if unknown:
        raise ValueError(f"{where}: `known_fault` has unknown key(s) {', '.join(unknown)}; "
                         f"use {', '.join(KNOWN_FAULT_KEYS)}")
    if not isinstance(kf.get("why"), str) or not kf["why"].strip():
        raise ValueError(f"{where}: `known_fault` needs `why` (why repair is not ours)")
    for k in ("match", "until"):
        if kf.get(k) is not None and not isinstance(kf[k], str):
            raise ValueError(f"{where}: `known_fault.{k}` is text")
    if kf.get("match") is not None:
        try:
            re.compile(kf["match"])
        except re.error as e:
            raise ValueError(f"{where}: `known_fault.match` is not a valid regex: {e}") from None
    if kf.get("until") is not None:
        from .ends import parse_time
        at = parse_time(kf["until"])
        if at is None:
            raise ValueError(f"{where}: `known_fault.until` is an ISO time (UTC unless it names a zone)")
        if at > time.time() + KNOWN_FAULT_S:
            raise ValueError(f"{where}: `known_fault.until` is at most {KNOWN_FAULT_S // 86400} d ahead")


def known_fault(spec: dict) -> dict | None:
    """The block's known fault as {why, match?, until?}; None without one."""
    kf = spec.get("known_fault")
    if not kf:
        return None
    return {"why": kf} if isinstance(kf, str) else kf


def _known_key(kf: dict) -> str:
    """Names the entry in check state: a changed entry (new wording, match or until) starts anew."""
    return json.dumps(kf, sort_keys=True)


def known_end(kf: dict, st: dict) -> float | None:
    """When the known fault stops covering failures: its `until`, else KNOWN_FAULT_S after it was first
    applied (recorded in check state); None while an entry without `until` was never applied."""
    if kf.get("until"):
        from .ends import parse_time
        return parse_time(kf["until"])
    if st.get("known_key") == _known_key(kf) and st.get("known_since"):
        return float(st["known_since"]) + KNOWN_FAULT_S
    return None


def _known_covers(kf: dict, st: dict) -> bool:
    """The known fault's `match` (if any) finds the last check output."""
    return not kf.get("match") or bool(re.search(kf["match"], st.get("last_out") or ""))


def _known_live(spec: dict, st: dict, now: float) -> str:
    """The known fault's `why` while it covers the last check output and its end has not passed, else ""."""
    kf = known_fault(spec)
    if not kf or not _known_covers(kf, st):
        return ""
    end = known_end(kf, st)
    return kf["why"] if end is None or now < end else ""


def of(payload: dict | None) -> dict | None:
    """A schedule payload's heal block, validated; None when it has none or it does not check out."""
    block = (payload or {}).get("heal")
    if not block:
        return None
    try:
        return validate(block)
    except ValueError:
        return None


# Checks -------------------------------------------------------------------------------------------
def _env() -> dict:
    from .providers.base import service_path
    return {**os.environ, "PATH": service_path()}


def _exec(args: str | list[str], cwd: str, timeout: float, env: dict | None = None) -> tuple[int, str]:
    """Run in a session of its own; a timeout kills the whole group (a hung ssh, a fix's children). stdin
    is /dev/null, never the daemon's (an open pipe would hold a command that reads it to its timeout)."""
    try:
        proc = subprocess.Popen(args, shell=isinstance(args, str), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=cwd, env=env or _env(), start_new_session=True)
    except OSError as e:
        return 127, str(e)
    schedrun.spawned(proc.pid)   # a daemon that stops or restarts meanwhile kills the group
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:   # a child that left the group still holds the pipes
            pass
        return 124, f"timed out after {timeout:.0f} s"
    finally:
        schedrun.ended(proc.pid)
        schedrun.check_stopped()   # killed by a daemon stop: no result (a fix rechecks at the next start)
    return proc.returncode, ((out or "") + (err or "")).strip()[-2000:]


def _shell(cmd: str, cwd: str, timeout: float, env: dict | None = None) -> tuple[int, str]:
    return _exec(cmd, cwd, timeout, env)


def _systemctl(spec: dict, *args: str) -> str:
    cmd = "systemctl " + ("--user " if spec.get("user") else "") + " ".join(shlex.quote(a) for a in args) \
        + " " + shlex.quote(spec["unit"])
    return f"ssh -o BatchMode=yes {shlex.quote(spec['host'])} {shlex.quote(cmd)}" if spec.get("host") else cmd


def _check_systemd(spec: dict, state: dict, cwd: str) -> tuple[int, str]:
    rc, out = _shell(_systemctl(spec, "show", "-p", "ActiveState", "-p", "NRestarts"), cwd, spec["timeout_s"])
    if rc == 255 and spec.get("host"):
        return 255, f"ssh failed: {out[-300:]}"
    if rc != 0:
        return 75, f"systemctl show failed (rc {rc}): {out[-300:]}"
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    active = props.get("ActiveState", "")
    try:
        restarts = int(props.get("NRestarts", "0"))
    except ValueError:
        restarts = 0
    before = state.get("nrestarts")
    state["nrestarts"] = restarts
    if active != "active":
        return 1, f"{spec['unit']} is {active or 'unknown'}"
    if before is not None and restarts > int(before):
        return 1, f"{spec['unit']} is crash-looping: NRestarts {before} -> {restarts}"
    return 0, f"{spec['unit']} is active (NRestarts {restarts})"


def _check_http(spec: dict, state: dict, cwd: str) -> tuple[int, str]:
    expect = spec.get("expect")
    try:
        with urllib.request.urlopen(spec["url"], timeout=min(spec["timeout_s"], 30)) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 1, f"{spec['url']} does not answer: {getattr(e, 'reason', e)}"
    ok = code in expect if expect else 200 <= code < 400
    return (0 if ok else 1), f"{spec['url']} answered {code}"


def _field(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _check_broker(spec: dict, state: dict, cwd: str) -> tuple[int, str]:
    rc, out = _shell(spec["status_command"], cwd, spec["timeout_s"])
    if rc in UNKNOWN_RCS:
        return rc, f"status command could not tell (rc {rc}): {out[-300:]}"
    if rc != 0:
        return 1, f"status command failed (rc {rc}): {out[-300:]}"
    try:
        data = json.loads(out[out.find("{"):] if "{" in out else out)
    except ValueError:
        return 75, f"status command printed no JSON: {out[-300:]}"
    problems = []
    state["held"] = bool(_field(data, spec.get("hold_field") or BROKER_HOLD))
    if state["held"]:
        problems.append(f"{spec.get('hold_field') or BROKER_HOLD} is set")
        if spec.get("holder_field") and not _field(data, spec["holder_field"]):
            problems.append(f"{spec['holder_field']} is empty\n{UNTENANTED}")
    missing, off = [], False
    for f in spec.get("auto_fields") or BROKER_AUTO:
        v = _field(data, f)
        if v is None:
            missing.append(f)
        elif not v:
            problems.append(f"{f} is off")
            off = True
    events = _field(data, spec["recovery_off_field"]) if spec.get("recovery_off_field") else None
    if (isinstance(events, list) and events) or events is True or (
            isinstance(events, (int, float)) and not isinstance(events, bool) and events > 0):
        # The service's own runtime report of a disabled recovery step: it counts whatever the flags say.
        problems.append(f"{spec['recovery_off_field']} reports recovery disabled: {json.dumps(events)[:200]}")
        off = True
    for option, field in (("expected_count", "count"), ("max_recent_failures", "recent_failures")):
        if option not in spec:
            continue
        value = _field(data, field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            missing.append(field)
        elif (value != spec[option] if option == "expected_count" else value > spec[option]):
            problems.append(f"{field} is {value} ({option} {spec[option]})")
    if problems:
        return 1, "; ".join(problems) + (f"\n{RECOVERY_OFF}" if off else "")
    if missing:
        return 75, f"status does not report {', '.join(missing)}"
    return 0, "no hold; automatic recovery on"


_PRESET_CHECKS: dict[str, Callable[[dict, dict, str], tuple[int, str]]] = {
    "systemd": _check_systemd, "http": _check_http, "broker": _check_broker}


def check(spec: dict, state: dict, cwd: str, env: dict | None = None) -> tuple[int, str]:
    """Run the check: (0 healthy | 1 unhealthy | other: unknown, its output). May update `state`.
    `env` is its project's (project.command_env), so a check calling `ttp lock` finds the project."""
    job = state.get("fix_job")
    if job and fix_running(job):   # its detached fix holds the resource: nothing to tell until it ends
        return 75, f"busy: its detached fix is running ({time.time() - float(job.get('started') or 0):.0f} s)"
    fn = _PRESET_CHECKS.get(spec.get("preset") or "")
    if fn and not spec.get("check"):
        return fn(spec, state, cwd)
    end = time.monotonic() + spec["timeout_s"]
    said: list[str] = []
    rc, out = _capped(spec, scriptwatch.rerun(lambda: _shell(spec["check"], cwd, max(end - time.monotonic(), 1), env),
                                              lambda r: r[0] != 0, spec["check"], cwd, env or _env(), said.append,
                                              again=lambda: end - time.monotonic() >= 1))
    return rc, "\n".join([out, *said]) if said else out


def finding(rc: int, out: str) -> bool:
    """A check result that is a non-outage finding: non-zero, with a FINDING line in its output."""
    return rc != 0 and any(line.strip() == FINDING for line in (out or "").splitlines())


def known_run(rc: int, out: str) -> tuple[str, str] | None:
    """(owner, why) of a run that exits 75 or 1 with a KNOWN_RE line of its own (module doc), else None;
    never for a run reporting automatic recovery off (RECOVERY_OFF): no owner covers that."""
    if rc not in (1, 75) or recovery_off(out):
        return None
    for line in (out or "").splitlines():
        m = KNOWN_RE.fullmatch(line.strip())
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return None


def classify(rc: int, out: str) -> str:
    """The word for a check result, as the daemon reads it: known (owner: why), healthy, unhealthy (not an
    outage when a finding), else unknown. `ttp heal test` prints it."""
    owned = known_run(rc, out)
    if owned:
        return f"known ({owned[0]}: {owned[1]})"
    if recovery_off(out):
        return "unhealthy, automatic recovery off"
    if finding(rc, out):
        return "unhealthy, not an outage"
    return {0: "healthy", 1: "unhealthy"}.get(rc, "unknown")


def recovery_off(out: str) -> bool:
    """A check result that reports automatic recovery switched off: a RECOVERY_OFF line in its output."""
    return any(line.strip() == RECOVERY_OFF for line in (out or "").splitlines())


def untenanted(out: str) -> bool:
    """A check result that reports a device held with no tenant: an UNTENANTED line in its output."""
    return any(line.strip() == UNTENANTED for line in (out or "").splitlines())


def route_failed(rc: int, out: str) -> bool:
    """An unknown check result that only says its route (ssh, a tunnel) failed: ROUTE_RC, or ROUTE_RE in its output."""
    return rc not in (0, 1) and (rc == ROUTE_RC or ROUTE_RE.search(out or "") is not None)


def _pings(host: str, cwd: str) -> bool | None:
    """The host answers one ping within PING_S: True, False when no reply came, None when ping itself failed
    (not installed, a name that does not resolve: that tells nothing about the box)."""
    darwin = sys.platform == "darwin"
    wait = ["-t" if darwin else "-w", str(PING_S)]   # the whole run's deadline in seconds
    rc = _exec(["ping", "-c", "1", *wait, host], cwd, PING_S + 5)[0]
    return True if rc == 0 else False if rc == (2 if darwin else 1) else None


def box_silent(spec: dict, cwd: str) -> str:
    """After ssh exited 255: why the box itself looks off (`ping_host` silent while `ping_peer` answers), or ""
    when it may still be our route (no ping options, the box answers, the peer is silent too, or ping could
    not tell)."""
    box, peer = spec.get("ping_host"), spec.get("ping_peer")
    if not (box and peer) or _pings(box, cwd) is not False or _pings(peer, cwd) is not True:
        return ""
    return f"{box} does not answer ping while {peer} does: it is off or rebooting"


def _unknown_suppressed(host: Any, name: str, spec: dict, st: dict, cwd: str, env: dict) -> bool:
    """An unknown stretch past its escalation point, on a block whose `suppress` probe says the resource is
    down on purpose: no escalation. Logged once per stretch (st["suppressed"]), cleared when the probe says no."""
    if not spec.get("suppress"):
        return False
    if not suppressed(host, name, spec, cwd, env, quiet=bool(st.get("suppressed"))):
        st.pop("suppressed", None)
        return False
    st["suppressed"] = True
    return True


def suppressed(host: Any, name: str, spec: dict, cwd: str, env: dict, quiet: bool = False) -> bool:
    """Whether the block's `suppress` probe says the resource is down on purpose (exit 0; module doc).
    Exit 1 says no; any other exit or a timeout is a broken probe: logged, and it heals normally."""
    rc, out = _exec(spec["suppress"], cwd, min(spec["timeout_s"], SUPPRESS_S), env)
    if rc == 0:
        if not quiet:
            _log(host, f"heal check {name}: suppressed: deliberately down")
        return True
    if rc != 1:
        tail = out.strip().splitlines()[-1][:200] if out.strip() else ""
        _log(host, f"heal check {name}: suppress probe broken (exit {rc}{': ' + tail if tail else ''}); healing normally")
    return False


def run_fix(spec: dict, cwd: str, env: dict) -> tuple[int, str]:
    """Run the fix in its project's `env` (project.command_env), under `ttp lock <resource>` when it
    names one. 75 only from the lock (busy or paused): `ttp lock` passes its command's exit code on,
    so a fix's own 75 comes back as FIX_OWN_75."""
    if not spec.get("resource"):
        return _capped(spec, _shell(spec["fix"], cwd, spec["timeout_s"], env))
    return _capped(spec, _exec(_fix_argv(spec), cwd, spec["timeout_s"] + LOCK_WAIT_S + 10, env))


def _capped(spec: dict, result: tuple[int, str]) -> tuple[int, str]:
    """`result` with " (cap; asked N)" after its "timed out after ... s" when `timeout_s` was capped."""
    rc, out = result
    if rc != 124 or "timeout_asked_s" not in spec or not out.startswith("timed out after "):
        return result
    from .schedule import capped_note
    first, nl, rest = out.partition("\n")
    return rc, first + capped_note(spec["timeout_asked_s"], MAX_TIMEOUT_S) + nl + rest


def _fix_argv(spec: dict) -> list[str]:
    if not spec.get("resource"):
        return ["sh", "-c", spec["fix"]]
    wrap = f'sh -c "$1"; rc=$?; [ "$rc" -eq 75 ] && exit {FIX_OWN_75}; exit "$rc"'
    return [sys.executable, "-m", "ttp", "lock", "--timeout", str(LOCK_WAIT_S), spec["resource"],
            "--", "sh", "-c", wrap, "heal-fix", spec["fix"]]


def fix_dir(p: Any, name: str) -> Path:
    """The folder of check `name`'s detached fix job (fix_detached, module doc)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60]
    return Path(p.state) / FIX_DIR / f"{safe}-{hashlib.sha1(name.encode()).hexdigest()[:8]}"


def start_fix(p: Any, name: str, spec: dict, cwd: str, env: dict, now: float) -> tuple[dict | None, str]:
    """Start the fix as a detached job (under `ttp lock <resource>` when it names one, so the lock is held
    while it runs): its record {rc, pid, started}, or None and why it could not start. Not registered with
    schedrun: a daemon stop or restart leaves it running."""
    from . import detach, locks
    from .runner import boot_id
    d = fix_dir(p, name)
    rc = d / "fix.rc"
    try:
        d.mkdir(parents=True, exist_ok=True)
        lock = locks.try_take([locks.job_lock(rc)], f"heal fix {name}", spec["fix"])
    except OSError as e:
        return None, str(e)
    if lock is None:
        return None, f"an earlier detached fix of {name} still runs ({d})"
    try:
        for f in (rc, rc.with_suffix(".log"), detach.lost(rc), detach.started(rc)):
            f.unlink(missing_ok=True)
        # The lock is the job's own, never one an outer `ttp lock` passed down to the daemon.
        env = {k: v for k, v in env.items() if k not in ("TTP_LOCKS_HELD", "TTP_LOCKS_SHARED")}
        pid = detach.start(rc, rc.with_suffix(".log"), _fix_argv(spec), cwd, env, lock)
    except OSError as e:
        return None, str(e)
    finally:
        lock.close()
    try:   # detach.state reads the boot from it, to tell a reboot from a crash
        durable_write(d / "detached.json", json.dumps([{"rc": str(rc), "pid": pid, "boot": boot_id(), "start": now}]))
    except OSError:
        pass
    return {"rc": str(rc), "pid": pid, "started": now}, ""


def fix_running(job: dict) -> bool:
    from . import detach
    return detach.state(Path(job["rc"]))[0] in ("running", "queued")


# State --------------------------------------------------------------------------------------------
def state(db: DB, name: str) -> dict:
    return dict(db.kv(STATE_KEY + name) or {})


def _save(db: DB, name: str, st: dict) -> None:
    db.set_kv(STATE_KEY + name, st)


def fixed_today(db: DB, name: str | None = None, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return sum(1 for x in db.kv(FIXED_KEY, []) or []
               if float(x.get("at") or 0) > now - 86400 and (name is None or x.get("name") == name))


def _record_fixed(db: DB, name: str, now: float) -> int:
    kept = [x for x in db.kv(FIXED_KEY, []) or [] if float(x.get("at") or 0) > now - 7 * 86400]
    db.set_kv(FIXED_KEY, kept + [{"at": now, "name": name}])
    return fixed_today(db, name, now)


def open_task(db: DB, name: str, label: str | None = None) -> dict | None:
    """The check's open self-fix task (its fingerprint is the label heal:<name>, or `label`). The
    labels are compared exactly (instr, then the parsed list): a LIKE pattern would read `_` and `%` in a name as wildcards."""
    want = LABEL + name if label is None else label
    for t in db.q(f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(OPEN))}) AND instr(labels, ?) > 0 "
                  f"ORDER BY id DESC", (*OPEN, json.dumps(want))):
        try:
            labels = json.loads(t["labels"] or "[]")
        except ValueError:
            continue
        if isinstance(labels, list) and want in labels:
            return t
    return None


def has_open_task(db: DB, name: str) -> bool:
    """A self-fix task of the check is open (its own, or the schedule_fix task it adopted)."""
    if open_task(db, name):
        return True
    tid = state(db, name).get("task")
    t = db.task(int(tid)) if tid else None
    return bool(t and t["status"] in OPEN)



def owns(db: DB, name: str) -> bool:
    """The check owns its failure: a self-fix task is open, or it holds (or escalated with) one. Its
    schedule then gets no schedule_fix task or schedule:<name> alert of its own: one per failure."""
    st = state(db, name)
    return has_open_task(db, name) or bool(st.get("task") or st.get("escalated"))

# The flow -----------------------------------------------------------------------------------------
def run(host: Any, name: str, spec: dict, now: float | None = None) -> tuple[str, float | None]:
    """One run of the check for the daemon `host` (it has `p` and `alert`, and may have `_progress`,
    the watchdog ping, called between the check and the fix). Returns (schedule status, when to run
    again if sooner than its period)."""
    now = time.time() if now is None else now
    streak = int(state(host.p.db, name).get(STREAK_KEY) or 0)
    out = _run(host, name, spec, now)
    st = state(host.p.db, name)   # only a plain healthy run keeps the streak going (module doc: backing off)
    if out[0] == "ok (healthy)" and not _timed_out():
        st[STREAK_KEY] = streak + 1
    else:
        st.pop(STREAK_KEY, None)
    _save(host.p.db, name, st)
    if STREAK_KEY in st and _timed_out():   # the daemon's timeout (end_streak) came while it was saved
        end_streak(host.p.db, name)
    try:
        _report_machine(host, name, spec, now)
    except Exception as e:   # the ledger is shared and best effort: the check's own result stands
        print(f"heal {name}: machine ledger report failed: {e}", file=sys.stderr)
    return out


def _timed_out() -> bool:
    """The daemon stopped the schedule run on this thread (its timeout recorded it failed, or a stop):
    whatever it returns late must not extend the healthy streak."""
    run = schedrun.current()
    return run is not None and run.stopping.is_set()


def end_streak(db: DB, name: str) -> None:
    """The check's run ended without a plain healthy result heal.run saw (a schedule timeout, a crash,
    a failing own command): its next run comes at `every` (module doc: backing off)."""
    st = state(db, name)
    if STREAK_KEY in st:
        st.pop(STREAK_KEY)
        _save(db, name, st)


def interval(db: DB, row: dict, spec: dict) -> int:
    """The schedule's period in force: `healthy_every` once the check was healthy `healthy_runs` times in a
    row, else its `every` (module doc: backing off)."""
    every = int(row["every_s"])
    slow = healthy_every_s(spec)
    if not slow or int(state(db, row["name"]).get(STREAK_KEY) or 0) < spec.get("healthy_runs", HEALTHY_RUNS):
        return every
    return max(slow, every)


def machine_of(spec: dict, known: dict | None = None) -> str | None:
    """The machine alias a check watches, for the ledger: `machine`, else the alias its `resource` is on
    when it watches a whole box (`outage`) or a broker."""
    if spec.get("machine"):
        return spec["machine"]
    res = str(spec.get("resource") or "")
    if not res or not (spec.get("outage") or spec.get("preset") == "broker"):
        return None
    if known is None:
        from . import machines
        known = machines.load()
    return next((a for a in sorted(known, key=len, reverse=True)
                 if res == a or res.startswith((a + "-", a + ":"))), None)


def known_outage(spec: dict, now: float) -> tuple[str, dict] | None:
    """(alias, record) of a known machine-ledger outage on the check's machine or `resource`, else None."""
    alias, res = machine_of(spec), str(spec.get("resource") or "")
    try:
        from . import machine_ledger
        outs = machine_ledger.outages(now)
    except Exception:   # the ledger is shared and best effort
        return None
    return next(((a, o) for a, o in sorted(outs.items(), key=lambda x: len(x[0]), reverse=True)
                 if a == alias or res == a or res.startswith((a + "-", a + ":"))), None)


OUTAGE_DARK = ("host", "power", "network")   # covers that explain a check unable to reach the box


def outage_explains(rec: dict, name: str, spec: dict, rc: int, out: str) -> bool:
    """Whether a known outage `rec` explains this result (module doc): always without `covers`, else only a
    covered word (a plural too) in the check's identity (name, `resource`, `unit`), never in its output, or
    OUTAGE_DARK for an unknown run (UNKNOWN_RCS) that is not a finding (the box is up)."""
    covers = [str(c).lower() for c in rec.get("covers") or []]
    if not covers:
        return True
    if rc in UNKNOWN_RCS and not finding(rc, out) and set(covers) & set(OUTAGE_DARK):
        return True
    ident = " ".join(str(x) for x in (name, spec.get("resource"), spec.get("unit")) if x).lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(c)}s?(?![a-z0-9])", ident) for c in covers)


def _fresh(st: dict, *also: str) -> None:
    """Start a new unhealthy episode: grace again, no task, nothing escalated (module doc). Keeps `fixes`."""
    for key in ("since", "task", "escalated", "observed", "why", "phase", "unknown_told", "paged", "paged_unknown",
                "told_findings", *also):
        st.pop(key, None)


def _quiet_outage(host: Any, name: str, st: dict, known: tuple[str, dict], now: float) -> tuple[str, float | None]:
    """Unhealthy under a known machine-ledger outage: like a known fault, no fix, no task, no alert."""
    from . import alerts
    db = host.p.db
    alias, rec = known
    until = timefmt.long(float(rec["until"]), host.p)
    alerts.close_key(db, STATE_KEY + name, f"known outage of {alias} until {until}: {rec.get('why') or ''}", now)
    for key in ("task", "escalated", "alerted", "observed", "phase", "paged"):
        st.pop(key, None)
    st["known_outage"] = alias
    _save(db, name, st)
    return f"unhealthy (known outage until {until})", None


def _report_machine(host: Any, name: str, spec: dict, now: float) -> None:
    """Tell the machine ledger what the check sees of its machine (module doc): down or held, or cleared."""
    alias = machine_of(spec)
    db = host.p.db
    st = state(db, name)
    was = st.get("machine_reported")
    want, grace = None, min(spec["grace_s"], OUTAGE_S) if spec.get("outage") else spec["grace_s"]
    if "unknown_since" in st:
        # Unknown tells nothing until an outage check escalates on it, once its window has passed.
        if not spec.get("outage") or now - float(st["unknown_since"]) < min(spec["grace_s"] or OUTAGE_S, OUTAGE_S):
            return
        # Only the route failed: this project's own problem, so nothing is reported (an earlier report clears).
        want, grace = [alias, spec.get("condition") or "down"] if alias and not st.get("route_only") else None, 0
    elif st.get("finding"):
        pass   # the box is up: nothing to report, and an earlier down report clears
    elif alias and st.get("status") == "unhealthy" and (spec.get("outage") or not _known_fault(spec, st, now)):
        if spec.get("condition"):
            want = [alias, spec["condition"]]
        elif st.get("held"):
            want = [alias, "held"]
        elif spec.get("preset") != "broker":   # a broker only missing its automatic recovery is not down
            want = [alias, "down"]
    if st.get("resource_paused"):
        want = None   # its own resource is paused: the outage is known here, nothing is reported
    if not was and not want:
        return
    from . import machine_ledger
    if was and was != want:
        machine_ledger.report(host.p.name, was[0], was[1], cleared=True, now=now)
    if want:
        machine_ledger.report(host.p.name, want[0], want[1], f"heal check {name}: {st.get('last_out') or ''}",
                              grace_s=grace, now=now, known=_known_live(spec, st, now),
                              escalate=escalate_to(host, spec))
    if want != was:
        st = state(db, name)
        if want:
            st["machine_reported"] = want
        else:
            st.pop("machine_reported", None)
        _save(db, name, st)


def _run(host: Any, name: str, spec: dict, now: float) -> tuple[str, float | None]:
    p, db = host.p, host.p.db
    st = state(db, name)
    if escalate_to(host, spec) == "coordinator":
        from . import alerts
        alerts.close_key(db, STATE_KEY + name, "escalation moved to coordinator", now)
        st.pop("alerted", None)
    if st.get("fix_job"):
        ran = _fix_job(host, name, spec, st, now)
        if ran is not None:
            return ran
    cwd, env = str(p.root), command_env(p)
    if st.get("phase") == "fixing" and now < float(st.get("fix_started") or 0) + spec["settle_s"]:
        # A restart cut the fix short: its recheck still waits for settle_s.
        return "unhealthy (fix interrupted; rechecking)", float(st["fix_started"]) + spec["settle_s"]
    from . import pauseends
    pauses = pauseends.owned(p)
    rc, out = check(spec, st, cwd, env)
    off = recovery_off(out)
    if off:   # recovery switched off is unhealthy whatever the exit code, and no exemption covers it
        rc = 1
        st["recovery_off"] = True
    else:
        st.pop("recovery_off", None)
    pauseends.health_result(p, name, rc, now, pauses)
    ping = getattr(host, "_progress", None)
    if callable(ping):
        ping()
    was_rc = st.get("last_rc")
    st.update(last_check=now, last_rc=rc, last_out=out[-1000:])
    _untenanted_hold(host, name, spec, st, rc, out, now)
    live = known_outage(spec, now)
    # An outage covering other parts: this result is handled normally, in the episode it is in.
    known = live if live and not off and outage_explains(live[1], name, spec, rc, out) else None
    if known:
        st["known_outage"] = known[0]
    elif st.pop("known_outage", None) and not live:   # the known outage ended: a check still failing gets its grace,
        t = db.task(int(st["task"])) if st.get("task") else None
        keep = st.get("task") if t is not None and t["status"] in OPEN else None   # no second task
        _fresh(st, "unknown_since", "route_only")   # an unknown one a new stretch
        if keep:
            st["task"] = keep
    owned = known_run(rc, out)
    if owned:   # a known, owned state: ends the unknown stretch; no cap message, fix or task
        for key in ("unknown_since", "route_only", "unknown_told", "paged_unknown", "suppressed"):
            st.pop(key, None)
        st["known_run"] = f"{owned[0]}: {owned[1]}"[:300]
        _save(db, name, st)
        return f"ok (known, {st['known_run'][:150]})", None
    st.pop("known_run", None)
    if finding(rc, out):   # the box is up: a plain unhealthy result of a check without `outage`
        rc, spec, st["finding"] = 1, {**spec, "outage": False}, True
    else:
        st.pop("finding", None)
    pending = st.get("phase") in ("fixing", "settling")   # a fix ran (or a restart cut it short)
    paused = _paused_resource(db, spec)
    if paused:
        st["resource_paused"] = paused
    else:
        st.pop("resource_paused", None)
        st.pop("pause_asked", None)
    if rc == 0:
        was = st.get("status")
        nxt = {k: st[k] for k in ("fixes", "nrestarts", "last_check", "last_rc", "last_out", "machine_reported",
                                  "hold_since", "hold_clear_since", "known_key", "known_since", "known_ended")
               if k in st}   # a flapping fault keeps its end
        _save(db, name, {**nxt, "status": "healthy"})
        t = db.task(int(st["task"])) if st.get("task") else None
        if t is not None and t["status"] == "queued":   # a running one is left to finish
            db.update_task(t["id"], status="cancelled", blocked_reason="check recovered before the fix ran")
        if pending and was == "unhealthy":
            n = _record_fixed(db, name, now)
            db.post("out", f"Self-healed {name}: its fix worked ({n} today). {out[:200]}".strip(), chat=None,
                    kind="info", severity="low", ref=STATE_KEY + name)
            return f"fixed ({n} today)", None
        return "ok (healthy)", None
    if rc != 1:
        # route_only: every unknown run since unknown_since was only its route failing (and the box not silent).
        route = route_failed(rc, out)
        silent = box_silent(spec, cwd) if rc == ROUTE_RC else ""
        if silent:
            st["last_out"] = (out[-800:] + f"\nping: {silent}").strip()
        st["route_only"] = route and not silent and bool(st.get("route_only", "unknown_since" not in st))
        st["unknown_since"] = st.get("unknown_since") or now
        again = None
        if spec.get("outage"):
            # A whole box whose check cannot even tell (ssh fails: it looks like this when it is down)
            # serves nothing either: escalated as unhealthy past the outage window, never fixed blind.
            window = min(spec["grace_s"] or OUTAGE_S, OUTAGE_S)
            dark = now - float(st["unknown_since"])
            if dark >= window and _unknown_suppressed(host, name, spec, st, cwd, env):
                _save(db, name, st)
                return "ok (unknown, suppressed: deliberately down)", None
            if dark >= window:
                st["status"] = "unhealthy"
                st.setdefault("since", st["unknown_since"])
                if known:
                    return _quiet_outage(host, name, st, known, now)
                if paused:
                    return _paused_outage(host, name, st, paused, now)
                if st.get("task") or st.get("escalated"):
                    return _escalated(host, name, spec, st, now)
                if st["route_only"]:
                    return _escalate(host, name, spec, st, now, f"its route to the box or resource failed for "
                                     f"{dark / 60:.0f} min (exit {rc}): this project's own route problem, so the "
                                     f"machine is not reported down")
                return _escalate(host, name, spec, st, now, f"its check could not tell for {dark / 60:.0f} min "
                                                            f"(exit {rc}): " + (silent or "the box or resource may be "
                                                                                "unreachable"))
            again = float(st["unknown_since"]) + window
        told = ""
        cap = float(spec.get("unknown_cap_s", DEFAULTS["unknown_cap_s"]) or 0)
        if (not spec.get("outage") and not known and cap and not st.get("unknown_told")
                and now - float(st["unknown_since"]) >= cap and not _unknown_suppressed(host, name, spec, st, cwd, env)):
            st["unknown_told"] = now   # once per stretch: the next healthy or unhealthy result ends it
            told = "; " + _tell_unknown(host, name, spec, st, rc, out, now)
        _save(db, name, st)
        if rc in UNKNOWN_RCS:
            return f"ok (unknown, rc {rc}{told})", again
        return f"error: heal check exited {rc}{told}: {out[-150:]}", again
    dark = now - float(st.get("unknown_since") or now)
    for key in ("unknown_since", "route_only", "unknown_told", "paged_unknown", "suppressed"):
        st.pop(key, None)
    if dark > max(_grace(spec), FRESH_UNKNOWN_S) and was_rc in UNKNOWN_RCS:   # not a broken check
        t = db.task(int(st["task"])) if st.get("task") else None
        if t is None or t["status"] not in OPEN:
            _fresh(st)   # back from a long unknown stretch with no open task: a new episode, same fix cap
    st["status"] = "unhealthy"
    since = st.setdefault("since", now)
    down = now - float(since)
    if known:
        return _quiet_outage(host, name, st, known, now)
    kf = known_fault(spec)
    if kf and not off and _confirmed(spec, st, now) and _known_covers(kf, st):
        key = _known_key(kf)
        if st.get("known_key") != key:   # first applied (or changed): its implicit end counts from now
            st.update(known_key=key, known_since=now)
            st.pop("known_ended", None)
        end = known_end(kf, st)
        if end is not None and now >= end and st.get("known_ended") != key:
            st["known_ended"] = key
            _known_ended(db, name, kf, st, now)
    if _known_fault(spec, st, now):
        # A self-fix may identify a known fault while its task is still open.
        from . import alerts
        alerts.close_key(db, STATE_KEY + name, f"known_fault applies to current failure: {kf['why']}", now)
        for key in ("task", "escalated", "alerted", "observed", "paged", "told_findings"):
            st.pop(key, None)
        _save(db, name, st)
        return "unhealthy (known fault: kept quiet)", None
    if paused:
        # Choice: a pause the project set on the check's own resource still quiets a recovery-off result.
        # The pause is the owner's explicit word and nothing is fixed under it; it is the one exemption
        # (unlike owner lines, outages and known_fault), and the paused-quiet ask still reaches the coordinator.
        return _paused_outage(host, name, st, paused, now)
    if st.get("task") or st.get("escalated"):
        return _escalated(host, name, spec, st, now, cwd, env)
    if pending:
        st["phase"] = None
        return _escalate(host, name, spec, st, now, f"the fix ran but the check still fails {down / 60:.0f} min on")
    grace = _grace(spec)
    if down < grace:
        _save(db, name, st)
        return f"unhealthy ({down:.0f} s, grace {grace:.0f} s)", float(since) + grace
    if spec.get("suppress") and suppressed(host, name, spec, cwd, env):
        _save(db, name, st)
        return "unhealthy (suppressed: deliberately down)", None
    if not spec.get("fix"):
        return _escalate(host, name, spec, st, now, "it has no fix")
    window = spec["window_h"] * 3600
    fixes = [t for t in st.get("fixes", []) if float(t) > now - window]
    if len(fixes) >= spec["max_fixes"]:
        st["fixes"] = fixes
        return _escalate(host, name, spec, st, now,
                         f"{len(fixes)} fixes in {spec['window_h']:g} h used up its cap ({spec['max_fixes']})")
    # Recorded before the fix runs: a restart in the middle still counts it, and rechecks next.
    deferred, deferred_on, told = (st.pop(k, None) for k in ("deferred_since", "deferred_on", "deferred_told"))
    st.update(fixes=fixes + [now], phase="fixing", fix_started=now)
    if spec.get("fix_detached"):
        job, err = start_fix(p, name, spec, cwd, env, now)
        if job:
            st["fix_job"] = {**job, "fixes": fixes, "deferred": [deferred, deferred_on, told]}
            _save(db, name, st)
            return "unhealthy (detached fix started; rechecking when it ends)", now + FIX_POLL_S
        frc, fout = 127, f"the detached fix could not start: {err}"
    else:
        _save(db, name, st)
        frc, fout = run_fix(spec, cwd, env)
    return _fix_ended(host, name, spec, st, now, frc, fout, fixes, deferred, deferred_on, told)


def _fix_job(host: Any, name: str, spec: dict, st: dict, now: float) -> tuple[str, float | None] | None:
    """A run while the check has a detached fix (module doc): busy while it runs, its end handled like a
    fix's, or None once its record is stale (the check then runs as usual)."""
    from . import detach
    job = st["fix_job"]
    rc = Path(str(job.get("rc") or ""))
    if not job.get("rc"):
        st.pop("fix_job", None)
        return None
    started = float(job.get("started") or now)
    where, val = detach.state(rc)
    if where in ("running", "queued"):
        limit = float(spec.get("fix_timeout_s") or FIX_TIMEOUT_S)
        if now - started < limit:
            _save(host.p.db, name, st)
            return (f"unhealthy (detached fix running {now - started:.0f} s; rechecking when it ends)",
                    min(now + FIX_POLL_S, started + limit))
        pid = int(job.get("pid") or 0)
        if pid > 1:
            detach.stop(rc, pid, f"ran past fix_timeout_s ({limit:.0f} s): killed by the daemon")
        from .schedule import capped_note
        frc, fout = 124, (f"the detached fix ran past fix_timeout_s ({limit:.0f} s)"
                          f"{capped_note(spec.get('fix_timeout_asked_s'), limit)}: killed")
    elif where == "ended":
        try:
            frc = int(val)
        except ValueError:
            frc = 1
        try:
            fout = rc.with_suffix(".log").read_text(errors="replace").strip()[-2000:]
        except OSError:
            fout = ""
    else:
        frc, fout = 1, f"the detached fix ended without an exit code: {val}"
    st.pop("fix_job", None)
    deferred, deferred_on, told = (list(job.get("deferred") or []) + [None] * 3)[:3]
    fixes = [float(t) for t in job.get("fixes") or []]
    return _fix_ended(host, name, spec, st, now, frc, fout, fixes, deferred, deferred_on, told)


def _fix_ended(host: Any, name: str, spec: dict, st: dict, now: float, frc: int, fout: str, fixes: list,
               deferred: Any, deferred_on: Any, told: Any) -> tuple[str, float | None]:
    """The fix's result (it ran in the run, or as a detached job that ended): settle and recheck, defer
    on a busy or paused lock, or escalate."""
    db = host.p.db
    window = spec["window_h"] * 3600
    st.update(fix_rc=frc, fix_out=fout[-1000:])
    if frc == 75 and spec.get("resource"):
        # Only the lock exits 75 here (run_fix): the fix never started. Not counted; try again next run.
        st.update(fixes=fixes, phase=None)
        res = spec["resource"]
        if "is paused" in fout:   # it waits however long the pause lasts, but says so once past unknown_cap_s
            st.update(deferred_on="paused", deferred_since=deferred if deferred and deferred_on == "paused" else now)
            cap = float(spec.get("unknown_cap_s", DEFAULTS["unknown_cap_s"]) or 0)
            note = ""
            if told:
                st["deferred_told"] = told
            elif cap and now - float(st["deferred_since"]) >= cap:
                st["deferred_told"] = now
                note = "; " + _tell_paused(host, name, spec, st, res, now)
            _save(db, name, st)
            return f"unhealthy (fix deferred: {res} paused{note})", None
        st["deferred_since"] = deferred if deferred and deferred_on != "paused" else now
        if now - float(st["deferred_since"]) >= window:
            return _escalate(host, name, spec, st, now, f"its fix could not get the lock {res} for "
                                                        f"{(now - float(st['deferred_since'])) / 3600:.1f} h: it stayed busy")
        _save(db, name, st)
        return f"unhealthy (fix deferred: {res} busy)", None
    if frc == FIX_OWN_75:
        frc = st["fix_rc"] = 75   # the fix's own 75 (run_fix), counted like any failed fix
    if frc != 0:
        st["phase"] = None
        return _escalate(host, name, spec, st, now, f"the fix exited {frc}")
    st["phase"] = "settling"
    _save(db, name, st)
    return "unhealthy (fixed; rechecking)", now + spec["settle_s"]


def _grace(spec: dict) -> float:
    return min(spec["grace_s"], OUTAGE_S) if spec.get("outage") else spec["grace_s"]   # a whole box waits no longer


def _escalate(host: Any, name: str, spec: dict, st: dict, now: float, why: str) -> tuple[str, float | None]:
    """Queue the check's one self-fix task (or keep the open one)."""
    db = host.p.db
    # The daemon may have queued a schedule_fix task for the same failure (a check exiting an error
    # code): adopt it, one task per failing check.
    t = open_task(db, name) or open_task(db, name, f"schedule_fix:{name}")
    if t is None:
        fix = spec.get("fix") or "(none)"
        spec_text = (
            f"Heal check `{name}` is unhealthy and its automatic fix cannot repair it: {why}.\n\n"
            f"Check: {spec.get('check') or 'preset ' + str(spec.get('preset'))}\n"
            f"Last check (exit {st.get('last_rc')}): {st.get('last_out') or '(no output)'}\n"
            f"Fix: {fix}\n"
            + (f"Last fix (exit {st.get('fix_rc')}): {st.get('fix_out') or '(no output)'}\n"
               if st.get("fix_rc") is not None else "")
            + f"Unhealthy since {timefmt.long(float(st['since']), host.p)}.\n"
            + ("The check reports a non-outage finding: the box or resource is up.\n" if st.get("finding") else "")
            + ("Only the check's own route (ssh, a tunnel) failed: repair this project's route to the box; "
               "the machine was not reported down to the shared machine ledger.\n" if st.get("route_only") else "")
            + "\n"
            "Find the cause and repair it within the charter's restrictions, then make sure the check "
            f"passes (`ttp heal test {name}`). If the fix command itself is wrong, correct the heal block "
            "in harness/schedules.json. Report what you changed. Hand off blocked only for a missing "
            "credential or a step the charter forbids. Under a recorded known outage or known_fault, the check "
            "ending at exit 75 (known outage, unknown) is the expected end state: do not try to force a pass; "
            "hand off once the check reports the known outage.")
        if spec.get("self_fix_note"):
            spec_text += f"\n\nProject note for this check:\n{spec['self_fix_note']}"
        tid = db.add_task(f"Self-fix: heal check {name} failing", spec_text, kind="work", priority=1,
                          origin="daemon", labels=[LABEL + name])
    else:
        tid = t["id"]
    st.update(task=tid, escalated=now, why=why)
    _save(db, name, st)
    return f"unhealthy (self-fix task #{tid}: {why})", None


def _escalated(host: Any, name: str, spec: dict, st: dict, now: float, cwd: str | None = None,
               env: dict | None = None) -> tuple[str, float | None]:
    """Unhealthy with its self-fix task out: alert once that task failed, blocked or ended without
    making the check pass, and once more per new finding in the episode. `cwd` set: the `suppress`
    probe runs first (the unknown path that passes none has run it already)."""
    db = host.p.db
    tid = st.get("task")
    t = db.task(int(tid)) if tid else None
    if t is not None and t["status"] in OPEN:
        _save(db, name, st)
        return f"unhealthy (self-fix task #{tid} open)", None
    if cwd is not None and spec.get("suppress") and suppressed(host, name, spec, cwd, env or {}):
        _save(db, name, st)
        return "unhealthy (suppressed: deliberately down)", None
    how = f"self-fix task #{tid} {t['status']}" if t else f"self-fix task #{tid} is gone"
    # A finding not yet told in this episode (normalized, so counters and times do not count) is told
    # once more, under its own fingerprint; one told before stays quiet. Capped, so a check whose
    # output never repeats cannot page every run.
    seen = screen.fingerprint(STATE_KEY + name, st.get("last_out") or "")
    told = list(st.get("told_findings") or [])
    new = bool(told) and seen not in told and len(told) < FINDINGS_MAX
    if new:
        for key in ("paged", "observed", "alerted"):
            st.pop(key, None)
        how += "; the check now reports a different finding"
    if not told or new:
        st["told_findings"] = told + [seen]
    last = (st.get("last_out") or "")[:300]
    paged = _page(host, name, spec, st, "paged", f"Heal check {name} of project {host.p.name}, unhealthy since "
                  f"{timefmt.long(float(st.get('since') or now), host.p)}, still fails and its {how}. "
                  f"Last check: {last}", now)
    if paged:
        _save(db, name, st)
        return f"unhealthy ({how}; {paged} told)", None
    if escalate_to(host, spec) == "coordinator":
        fp = f"{STATE_KEY}{name}:{float(st.get('since') or st.get('escalated') or now):.0f}"
        if len(st["told_findings"]) > 1:
            fp += f":{st['told_findings'][-1][:8]}"
        if not st.get("observed") and not db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                 (now, "daemon", "observation", fp, "high",
                  f"Heal check {name} still fails and its {how}. Repair it within the charter "
                  f"(a new task, or a corrected heal block); the check clears itself once it passes. "
                  f"Last check: {last}", "queued"))
        st["observed"] = st.get("observed") or now
        _save(db, name, st)
        return f"unhealthy ({how}; coordinator told)", None
    if not st.get("alerted"):
        st["alerted"] = now
        host.alert(STATE_KEY + name, ("Heal check" if st.get("finding") or st.get("route_only") else "Outage: heal check")
                   + f" {name} still fails. Last check: {last} "
                   + outagemsg.facts(float(st.get("since") or now), now, outagemsg.TO_USER, host.p, task=tid,
                                     tried=_tried(spec, st) + [how],
                                     next_step="repair it or say how; the alert clears once the check passes"),
                   "high", **({"every_s": 0} if new else {}))
    _save(db, name, st)
    return f"unhealthy ({how}; alerted)", None


def _tried(spec: dict, st: dict) -> list[str]:
    """What the check's own automatic fix did, for an outage message (outagemsg.facts)."""
    return [f"its fix `{spec['fix']}` (exit {st.get('fix_rc')})"] if spec.get("fix") and st.get("fix_rc") is not None \
        else []


def _page(host: Any, name: str, spec: dict, st: dict, mark: str, text: str, now: float) -> str | None:
    """The block's `to` (pageto): one high daemon note to that other local project instead of the
    escalate_to() step, once per episode (`st[mark]`). Returns the project told, or None when there is
    no `to`, it names this project or cannot be resolved (logged), or the note did not go: then the
    escalation goes on as without `to`."""
    if spec.get("to") is None:
        return None
    from . import upstream
    target, why = pageto.resolve(spec["to"], machine_of(spec))
    if target is None or target == host.p.name:
        if target is None:
            _log(host, f"heal check {name}: not paged ({why}); escalated here instead")
        return None
    if st.get(mark):
        return target
    got = upstream.send(host.p.name, None, target, text, "high", now=now, sender="daemon")
    if got not in ("sent", "duplicate"):
        _log(host, f"heal check {name}: note to {target}: {got}; escalated here instead")
        return None
    st[mark] = now
    _log(host, f"heal check {name}: paged {target}")
    return target


def _log(host: Any, msg: str) -> None:
    try:
        from .daemon import log
        log(host.p, msg)
    except Exception:
        pass


def _observe(db: Any, fp: str, text: str, now: float) -> None:
    """Queue one high observation for the coordinator under `fp`, unless one is already there."""
    if not db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", "observation", fp, "high", text, "queued"))


def _tell_unknown(host: Any, name: str, spec: dict, st: dict, rc: int, out: str, now: float) -> str:
    """A check without `outage` that stayed unknown for unknown_cap_s: one message to escalate_to()
    with its last output and what blocks it. Returns who was told."""
    last = next((ln.strip() for ln in reversed((out or "").splitlines()) if ln.strip()), "")
    blocker = (f"exit {rc}" + (", only its route failing" if st.get("route_only") else "")
               + (f": {last[:200]}" if last else ""))
    text = (f"Heal check {name} could not tell for {(now - float(st['unknown_since'])) / 60:.0f} min ({blocker}); "
            f"nothing is fixed or escalated on it until it can. Last check: {(out or '(no output)')[-300:]}")
    paged = _page(host, name, spec, st, "paged_unknown", text.replace(
        f"Heal check {name} ", f"Heal check {name} of project {host.p.name}, unknown since "
        f"{timefmt.long(float(st['unknown_since']), host.p)}, ", 1), now)
    if paged:
        return f"{paged} told"
    if escalate_to(host, spec) == "coordinator":
        _observe(host.p.db, f"{STATE_KEY}{name}:unknown:{float(st['unknown_since']):.0f}",
                 text + " Repair the check or what blocks it within the charter.", now)
        return "coordinator told"
    host.alert(f"heal-unknown:{name}", text, "normal", every_s=0)
    return "user told"


def _pause_of(host: Any, res: str) -> dict:
    """The pause entry ({"reason", "since", "by", "project"}) holding the lock `res`, else {}."""
    try:
        paused = host.p.db.paused_resources()
        if res in paused:
            return paused[res]
        from . import locks as lk
        cfg = getattr(host, "cfg", None) or {}
        return next((v for k, v in paused.items() if lk.canonical(cfg, k) == lk.canonical(cfg, res)), {})
    except Exception:
        return {}


def _tell_paused(host: Any, name: str, spec: dict, st: dict, res: str, now: float) -> str:
    """A fix deferred on a paused resource for unknown_cap_s: a pause the user set gets one low line
    and no ask; any other (this project's own, or another project's shared one) one coordinator
    observation. Both name the pause, who set it and what lifting it allows. Returns who was told."""
    pz = _pause_of(host, res)
    by = str(pz.get("by") or "")
    user = by in ("", "user")   # a pause of unknown origin is treated as the user's: never lifted on our word
    who = "the user" if user else by + (f" of project {pz['project']}" if pz.get("project") else "")
    text = (f"Heal check {name} has failed since {timefmt.long(float(st.get('since') or now), host.p)} and its fix "
            f"has waited {(now - float(st['deferred_since'])) / 3600:.1f} h on {res}, paused by {who}"
            + (f" ({pz['reason']})" if pz.get("reason") else "")
            + (f" since {timefmt.long(float(pz['since']), host.p)}" if pz.get("since") else "")
            + f". Lifting the pause lets its fix run: {spec.get('fix')}.")
    if user:
        host.p.db.post("out", text, chat=None, kind="info", severity="low", ref=STATE_KEY + name)
        return "user's pause: told"
    _observe(host.p.db, f"{STATE_KEY}{name}:paused:{float(st['deferred_since']):.0f}",
             text + " Lift the pause once its reason is over, or repair the check another way within the charter.",
             now)
    return "coordinator told"


def _untenanted_s(host: Any) -> float:
    v = ((getattr(host, "cfg", None) or {}).get("heal") or {}).get("untenanted_hold_s")
    try:
        return max(0.0, float(v)) if v is not None and not isinstance(v, bool) else UNTENANTED_HOLD_S
    except (TypeError, ValueError):
        return UNTENANTED_HOLD_S


def _untenanted_hold(host: Any, name: str, spec: dict, st: dict, rc: int, out: str, now: float) -> None:
    """Track a device held with no tenant (module doc) and page once it outlasts heal.untenanted_hold_s,
    whatever outage, fault or mute keeps the rest of the check quiet. The alert's open episode keeps
    repeats quiet and the alerts sweep ends it (cleared_why with `hold`). Changes `st`; its caller saves it."""
    db = host.p.db
    if not untenanted(out):
        # The stretch is the episode: only answers without the line for HOLD_CLEAR_S end it, so a loop of
        # failing automatic resets that frees the device for a moment between runs never restarts its age.
        if rc in (0, 1) and "hold_since" in st:
            clear = float(st.setdefault("hold_clear_since", now))
            if now - clear >= HOLD_CLEAR_S:
                st.pop("hold_since", None)
                st.pop("hold_clear_since", None)
        return
    st.pop("hold_clear_since", None)
    since = float(st.setdefault("hold_since", now))
    ceiling = _untenanted_s(host)
    if now - since < ceiling or _hold_known(db, spec, name):
        return
    res = str(spec.get("resource") or "")
    what = machine_of(spec) or res or "its device"
    try:
        from . import machine_ledger
        known = machine_ledger.outages(now).get(machine_of(spec) or "")
    except Exception:   # the ledger is shared and best effort
        known = None
    to_coord = escalate_to(host, spec) == "coordinator"
    text = (f"Device held with no tenant: heal check {name} reports {what} held with no holder for "
            f"{(now - since) / 3600:.1f} h (ceiling {ceiling / 3600:g} h), so queued work cannot use it. "
            + (f"The known outage of {what} ({known.get('why') or 'no reason given'}) does not cover an "
               f"untenanted hold. " if known else "")
            + f"Last check (exit {rc}): {out[-300:]} "
            + outagemsg.facts(since, now, outagemsg.actor(host.p.name, "its coordinator") if to_coord
                              else outagemsg.TO_USER, host.p,
                              tried=_tried(spec, st),
                              next_step="find who holds it and release it, or record why it must stay"))
    if to_coord:
        fp = f"{HOLD_KEY}{name}:{since:.0f}"
        if not db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                 (now, "daemon", "observation", fp, "high", text, "queued"))
        return
    host.alert(HOLD_KEY + name, text, "high")


def _hold_known(db: Any, spec: dict, name: str) -> str:
    """Why a pause keeps the check's untenanted hold quiet: its `resource` is paused by the user or with a
    reason naming the check (_pause_names); else ""."""
    res = str(spec.get("resource") or "")
    try:
        pause = (db.paused_resources() or {}).get(res) if res else None
    except Exception:
        pause = None
    return f"{res} is paused ({'by the user' if pause.get('by') == 'user' else 'naming the check'})" \
        if pause and _pause_names(pause, name) else ""


def _paused_resource(db: Any, spec: dict) -> str:
    """The resource an outage check watches when this project paused it (its own or shared), else ""."""
    res = str(spec.get("resource") or "")
    if not spec.get("outage") or not res:
        return ""
    try:
        return res if res in db.paused_resources() else ""
    except Exception:
        return ""


def _pause_names(pause: dict, name: str) -> bool:
    """The user set the pause, or its recorded reason names the check: the outage is known by it."""
    if pause.get("by") == "user":
        return True
    return bool(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", str(pause.get("reason") or ""), re.I))


def _paused_quiet_s(host: Any) -> float:
    v = ((getattr(host, "cfg", None) or {}).get("heal") or {}).get("paused_quiet_s")
    try:
        return max(0.0, float(v)) if v is not None and not isinstance(v, bool) else PAUSED_QUIET_S
    except (TypeError, ValueError):
        return PAUSED_QUIET_S


def _paused_outage(host: Any, name: str, st: dict, res: str, now: float) -> tuple[str, float | None]:
    """An outage check whose own resource is paused: no fix, no user alert (an earlier one clears) and
    nothing reported to the machine ledger. Under a pause it is not known by (_pause_names), the
    coordinator is asked once per stretch after heal.paused_quiet_s (module doc); the pause stays."""
    from . import alerts
    db = host.p.db
    alerts.close_key(db, STATE_KEY + name, f"{res} is paused", now)
    st.pop("alerted", None)
    try:
        pause = db.paused_resources().get(res) or {}
    except Exception:
        pause = {}
    if _pause_names(pause, name):
        _save(db, name, st)
        return f"unhealthy ({res} paused: not escalated)", None
    start = max(float(st.get("since") or now), float(pause.get("since") or 0))
    quiet = _paused_quiet_s(host)
    if now - start < quiet:
        _save(db, name, st)
        return f"unhealthy ({res} paused: not escalated)", None
    fp = f"{STATE_KEY}{name}:paused:{start:.0f}"
    if st.get("pause_asked") != fp and not db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        reason = str(pause.get("reason") or "no recorded reason")
        who = f"project {pause['project']}" if pause.get("project") else str(pause.get("by") or "this project")
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", "observation", fp, "normal",
              f"Outage of {res} under a pause set for {reason} for {(now - start) / 3600:.0f} h: is the pause "
              f"still right? Heal check {name} still fails (last check: {(st.get('last_out') or '')[:300]}); "
              f"the pause ({who}) keeps it from being fixed, alerted or reported to the machine ledger. "
              f"Lift the pause if its reason no longer holds, or keep it with a reason naming {name} so the "
              f"check stays quiet under it.", "queued"))
    st["pause_asked"] = fp
    _save(db, name, st)
    return f"unhealthy ({res} paused: coordinator asked)", None


def escalate_to(host: Any, spec: dict) -> str:
    """Who hears of a check its self-fix task could not repair: the block's `escalate`, else the
    project's `heal.escalate`, else the user."""
    v = spec.get("escalate") or ((getattr(host, "cfg", None) or {}).get("heal") or {}).get("escalate")
    return v if v in ESCALATE else "user"


def _confirmed(spec: dict, st: dict, now: float) -> bool:
    """A confirmed failure a known fault may cover: never a prolonged outage or an unknown check."""
    return bool(st.get("status") == "unhealthy"
                and "unknown_since" not in st and (st.get("finding") or st.get("last_rc") == 1)
                and (st.get("finding") or not spec.get("outage")
                     or now - float(st.get("since", now)) < min(OUTAGE_S, spec["grace_s"] or OUTAGE_S)))


def _known_fault(spec: dict, st: dict, now: float) -> bool:
    """A live known fault (its match finds the output, its end has not passed) covers a confirmed failure,
    never one with automatic recovery switched off (RECOVERY_OFF)."""
    return not st.get("recovery_off") and _confirmed(spec, st, now) and bool(_known_live(spec, st, now))


def _known_ended(db: DB, name: str, kf: dict, st: dict, now: float) -> None:
    """A known fault's end passed while it still covers the failure: one low feed line, and one low
    observation so the coordinator renews or drops the entry. Normal handling resumes."""
    text = (f"Heal check {name}: its known_fault ({kf['why']}) ended, so its failure is handled normally "
            f"again. Renew it (a new `until`, at most {KNOWN_FAULT_S // 86400} d ahead) if repair is still "
            f"not ours, or drop it from the heal block. Last check: {(st.get('last_out') or '')[:300]}")
    db.post("out", text, chat=None, kind="info", severity="low", ref=STATE_KEY + name)
    fp = f"{STATE_KEY}{name}:known_fault:{float(st.get('known_since') or now):.0f}"
    if not db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", "observation", fp, "low", text, "queued"))


def cleared_why(db: DB, name: str, now: float | None = None, hold: bool = False) -> str | None:
    """Live reason the check's user alert ended (with `hold`, its untenanted-hold page `heal-hold:<name>`);
    None while it still needs the user."""
    now = time.time() if now is None else now
    row = db.one("SELECT payload FROM schedules WHERE name=? AND enabled=1", (name,))
    spec = of(json.loads(row["payload"] or "{}")) if row else None
    st = state(db, name)
    if hold:
        if not spec:
            return "the check has no enabled schedule with a heal block"
        if "hold_since" not in st:
            return "condition cleared"
    elif not spec or st.get("status") == "healthy":
        return "condition cleared"
    escalation = spec.get("escalate")
    if not escalation:
        # Readers use the project's current config without writing a last-good copy.
        from .project import layered
        base = Path(db.path).parent.parent
        for path in (base / "harness" / "project.json", base / "state" / "project.last-good.json"):
            try:
                cfg = json.loads(path.read_text())
                if not isinstance(cfg, dict):
                    continue
                escalation = (layered(cfg).get("heal") or {}).get("escalate")
                break
            except (OSError, ValueError):
                continue
    if escalation == "coordinator":
        return "escalation moved to coordinator"
    if hold:
        return _hold_known(db, spec, name) or None
    if _known_fault(spec, st, now):
        return f"known_fault applies to current failure: {_known_live(spec, st, now)}"
    return None


def is_outage(db: DB, name: str) -> bool:
    """The check watches a whole box or resource (`outage`) and its failure is not a non-outage finding
    nor only its own route failing."""
    row = db.one("SELECT payload FROM schedules WHERE name=? AND enabled=1", (name,))
    spec = of(json.loads(row["payload"] or "{}")) if row else None
    st = state(db, name)
    return bool(spec and spec.get("outage") and not st.get("finding") and not st.get("route_only"))


def holds(db: DB, name: str, now: float | None = None) -> bool:
    """A heal alert stays only while the check still requires user escalation."""
    return cleared_why(db, name, now) is None


# Views --------------------------------------------------------------------------------------------
def _secs(s: int) -> str:
    """An interval as `every` reads it: 1h, 30m, or 90s."""
    from .schedule import _every_text
    t = _every_text(int(s))
    return f"{t}s" if isinstance(t, int) else t


def checks(db: DB) -> list[tuple[dict, dict]]:
    """Enabled schedules with a heal block: (schedule row, heal spec)."""
    out = []
    for r in db.q("SELECT * FROM schedules WHERE enabled=1 AND kind='command' ORDER BY name"):
        spec = of(json.loads(r["payload"] or "{}"))
        if spec:
            out.append((r, spec))
    return out


def summary(db: DB, now: float | None = None) -> dict:
    n_ok = n_bad = n_unknown = 0
    for r, _ in checks(db):
        s = state(db, r["name"])
        if s.get("status") == "unhealthy" and s.get("escalated"):   # an unreachable box escalated too
            n_bad += 1
        elif "unknown_since" in s or "known_run" in s:   # the latest reading tells nothing either way
            n_unknown += 1
        elif s.get("status") == "unhealthy":
            n_bad += 1
        elif s.get("status") == "healthy":
            n_ok += 1
        else:
            n_unknown += 1
    return {"ok": n_ok, "fixed": fixed_today(db, now=now), "failing": n_bad, "unknown": n_unknown}


def line(db: DB, now: float | None = None) -> str:
    """'health: N ok, M fixed today, K failing' for ttp status and the web app; "" without checks."""
    if not checks(db):
        return ""
    s = summary(db, now)
    return (f"health: {s['ok']} ok, {s['fixed']} fixed today, {s['failing']} failing"
            + (f", {s['unknown']} unknown" if s["unknown"] else ""))


def digest_lines(db: DB, since: float, now: float | None = None) -> list[str]:
    """The digest's record of checks fixed since the last turn: nothing to do, for the record."""
    now = time.time() if now is None else now
    done = [x for x in db.kv(FIXED_KEY, []) or [] if float(x.get("at") or 0) > since]
    if not done:
        return []
    names = sorted({x["name"] for x in done})
    return ["## Self-healed (nothing to do): " + "; ".join(
        f"fixed {n}, {fixed_today(db, n, now)} today" for n in names)]


def describe(r: dict, spec: dict, db: DB, now: float | None = None) -> str:
    now = time.time() if now is None else now
    s = state(db, r["name"])
    what = spec.get("check") or f"preset {spec.get('preset')}"
    status = s.get("status") or "not checked yet"
    if "known_run" in s:
        status = f"known ({s['known_run']})"
    elif "unknown_since" in s:
        status = f"unknown (exit {s.get('last_rc')})"
    elif status == "unhealthy" and s.get("since"):
        status += f" for {(now - float(s['since'])) / 60:.0f} min"
    extra = f"; self-fix task #{s['task']}" if s.get("task") else ""
    slow = healthy_every_s(spec)
    if slow:
        iv, n = interval(db, r, spec), int(s.get(STREAK_KEY) or 0)
        extra += (f"; every {_secs(iv)} ({n} healthy in a row)" if iv > int(r["every_s"])
                  else f"; every {_secs(iv)}, {_secs(slow)} after {spec.get('healthy_runs', HEALTHY_RUNS)} "
                       f"healthy in a row ({n} so far)")
    return (f"{r['name']}: {status}{extra}; {fixed_today(db, r['name'], now)} fixed today; check: {what}; "
            f"fix: {spec.get('fix') or '(none)'}" + (f" under lock {spec['resource']}" if spec.get("resource") else ""))
