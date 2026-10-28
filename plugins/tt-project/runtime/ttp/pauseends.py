# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""End conditions on resource pauses.

A resource pause (`resource_pause`, `ttp pause --resource`) must say when it ends: `until` (a time
at most MAX_UNTIL_S ahead) and/or `end_when` (a read-only shell probe like `retry_when`: exit 0 =
over; 1, 75 or ssh's own 255 = not yet; a 255 whose host answers is broken, ends.reap_probes). It
may also name `report_from`, another project whose report it waits on. A coordinator pause of a resource with an enabled heal check gets that check's health
end (stored as `end_when: {heal, healthy_n, clean_s}`) unless it names a probe, which replaces it:
the daemon lifts it without a model after HEALTHY_N consecutive healthy checks over CLEAN_S
seconds; a gap longer than the check's freshness bound (a stopped daemon, a disabled check) starts
it over. Its `until`, if any, stays the deadline reported when it passes. Nobody writes a health end
or tunes its thresholds: callers' `end_when` is a probe string only, and the defaults apply (an
accepted gap). Any other pause gets the health end only when it names no end. For other ends,
when its time passes, its probe passes or breaks, the
daemon queues one `pause_end_due` event and the coordinator lifts the pause or extends it with a
reason. A note from the `report_from` project re-prompts it the same way. A pause the user set is
lifted only on their word: its end is a signal, never an end. Once it passes the pause stays, the
time is kept (PASSED_KEY; describe shows it) and the coordinator gets one low observation: tell the
user once, never say it is lifting. The exception is `user_end`: an `until` the user's own words gave
(a length or a time) is their word in advance, so at it the daemon lifts the pause with no model,
wakes its waiters and queues one `pause_user_end_lifted` event (USER_END_KIND); an `end_when` is the
coordinator's reading of their words and stays a signal. A broken probe, or a failed job it waits on, is told as an end
that can no longer be checked, never as passed. A pause recorded without
an end (from before ends were required) gets DEFAULT_S and one info message saying so.

Only the project that set a shared pause (shared.py) checks it, so one owner is prompted."""
from __future__ import annotations

import re
import json
import subprocess
import time
from typing import Any

from . import detach, ends, shared, sweep
from .db import PAUSED_RESOURCES_KEY, SHARED_SEEN_KEY
from .project import Project

MAX_UNTIL_S = 7 * 86400
DEFAULT_S = 7 * 86400
HEALTHY_N, CLEAN_S = 3, 1800   # a health end's thresholds: the same for every pause
END_FIELDS = ("until", "end_when", "report_from", "end_default", "user_end")
FLEET_PREFIX = "machine:"   # owned() keys a fleet hold this project checks by its machine
DUE_KEY = "pause_end_due"      # kv: {resource: end signature} whose end was reported to the coordinator
HEALTH_KEY = "pause_health"   # kv: consecutive check results fenced to each pause's identity
RELEASE_KEY = "pause_release"  # kv: shared pauses whose file removal began, so a crash cannot lose the receipt
PASSED_KEY = "pause_end_passed"  # kv: {resource: {"sig", "at"}}: when a user pause's end condition passed
USER_END_KIND = "pause_user_end_lifted"   # the event of a pause lifted at the end the user's own words gave
USER_END_WHY = "ended at the user's own end"
CHECK_EVERY_S = 60
PROBE_EVERY_S = ends.PROBE_EVERY_S
PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,79}")
NEEDS_END = ("a resource pause needs an end: `until` (a delay such as 2d or an ISO time, at most 7 days "
             "ahead) or `end_when` (a read-only shell probe)")


def from_action(a: dict, now: float | None = None, p: Project | None = None, by: str = "", where=None) -> dict:
    """Validate `until`, an `end_when` shell probe, `report_from` and `user_end` (only with `user_asked`
    true and an `until`: the end the user's own words gave). With `p`, a pause of a resource
    with an enabled heal check gets its health end: a pause `by` the coordinator unless it names a
    probe (its `until` stays the reported deadline), any other only when it names no end. A zone-less
    `until` is read in the home zone of `where` (else `p`; ends.parse_expires)."""
    now = time.time() if now is None else now
    out: dict = {}
    if a.get("until"):
        out["until"] = ends.parse_expires(a["until"], now, what="until", max_s=MAX_UNTIL_S,
                                          where=where if where is not None else p)
    probe = a.get("end_when")
    if probe is not None and not isinstance(probe, str):
        raise ValueError("end_when must be one shell command; a health end is never written by hand: a coordinator "
                         "pause of a resource with an enabled heal check gets it unless it names a probe")
    if probe and ("\n" in probe.strip() or len(probe) > ends.PROBE_CHARS):
        raise ValueError(f"end_when must be one shell command on one line, at most {ends.PROBE_CHARS} characters")
    if probe and probe.strip():
        ends.check_sh(probe.strip(), "end_when")
        ends.check_not_trivial(probe.strip(), "end_when")
        out["end_when"] = probe.strip()
    elif p is not None and (by == "coordinator" or not out.get("until")):
        health = health_end(p, a.get("resource"))
        if health:
            out["end_when"] = health
    who = str(a.get("report_from") or "").strip()
    if who:
        if not PROJECT_RE.fullmatch(who):
            raise ValueError(f"report_from {who!r} is not a project name")
        out["report_from"] = who
    if not (out.get("until") or out.get("end_when")):
        raise ValueError(NEEDS_END)
    flag = a.get("user_end")
    if flag is not None and not isinstance(flag, bool):
        raise ValueError("user_end must be true or false")
    if flag:
        if a.get("user_asked") is not True:
            raise ValueError("user_end needs `user_asked: true`: only an end the user's own words gave lifts "
                             "their pause by itself")
        if not out.get("until"):
            raise ValueError("user_end needs an `until` (the delay or time the user gave); an `end_when` probe "
                             "only signals the end of a user's pause")
        out["user_end"] = True
    return out


def health_end(p: Project, resource: str) -> dict | None:
    """The health end of the first enabled heal check that covers `resource`, or None."""
    from . import heal
    for row, spec in heal.checks(p.db):
        if resource and resource in (spec.get("resource"), spec.get("machine")):
            return {"heal": row["name"], "healthy_n": HEALTHY_N, "clean_s": CLEAN_S}
    return None


def describe(v: dict, where=None, passed: float | None = None, now: float | None = None) -> str:
    """'ends 2026-10-06 05:00 UTC, or when `probe` passes; waits on project x's report' (or ''). A
    pause the user set never ends by itself: its end is shown as a condition, and once it passed
    (`passed`, see passed_at, or an `until` before `now`) as passed, needing the user's word. Its
    `user_end` time is the exception: shown as the user's end, the time it lifts."""
    end = v.get("end_when")
    when = (f"when heal `{end['heal']}` has {end['healthy_n']} healthy results over {end['clean_s']:g} s"
            if isinstance(end, dict) else f"when `{end}` passes")
    parts = ([ends.shown(float(v["until"]), where)] if v.get("until") else []) + ([when] if end else [])
    out = "ends " + ", or ".join(parts) if parts else ""
    if v.get("by") == "user" and v.get("user_end") and v.get("until"):
        out = (f"set by the user; ends {ends.shown(float(v['until']), where)}, user's end"
               + (f"; end condition: {when}" if end else ""))
    elif v.get("by") == "user":
        now = time.time() if now is None else now
        if passed is None and v.get("until") and float(v["until"]) <= now:
            passed = float(v["until"])
        out = ("set by the user; " + (f"end condition passed at {ends.shown(passed, where)}; lifting needs the "
                                      f"user's word" if passed is not None else
                                      "ends only on the user's word" + (f"; end condition: {', or '.join(parts)}"
                                                                        if parts else "")))
    if v.get("end_default"):
        out += " (a default: it was set without an end)"
    if v.get("report_from"):
        out += f"; waits on project {v['report_from']}'s report"
    return out.strip("; ")


def passed_at(db, name: str, v: dict) -> float | None:
    """When the end condition of the user's pause `v` of `name` passed, if it did (PASSED_KEY)."""
    got = (db.kv(PASSED_KEY) or {}).get(name)
    return float(got["at"]) if isinstance(got, dict) and got.get("sig") == _sig(v) else None


def _sig(v: dict) -> str:
    return f"{v.get('by')}|{v.get('project')}|{v.get('generation')}|{v.get('since')}|{v.get('until')}|{v.get('end_when')}"


def _fresh_s(check) -> float:
    """How long a check's last result counts as current: past it, health is unknown."""
    return max(CHECK_EVERY_S, 2 * float(check["every_s"]))


def health_result(p: Project, name: str, rc: int, now: float, pauses: dict) -> None:
    """Record each actual check once, against the pauses present before it started. A check that
    fails or cannot tell resets the clean interval, as does a gap past its freshness bound since the
    last result (no continuous evidence). No tick can replay a result after a restart."""
    from . import heal
    check = next((r for r, _ in heal.checks(p.db) if r["name"] == name), None)
    with p.db.tx():
        current = owned(p)
        counts = {k: v for k, v in (p.db.kv(HEALTH_KEY) or {}).items() if k in current}
        for resource, pause in pauses.items():
            end = pause.get("end_when")
            if not isinstance(end, dict) or end.get("heal") != name or _sig(current.get(resource, {})) != _sig(pause):
                continue
            sig = _sig(pause)
            before = counts.get(resource, {})
            if rc != 0:
                counts.pop(resource, None)
                continue
            if before.get("sig") != sig or now <= before.get("last", -1) or check is None \
                    or now - before.get("last", now) > _fresh_s(check):
                before = {}
            counts[resource] = {"sig": sig, "n": before.get("n", 0) + 1,
                                "first": before.get("first", now), "last": now}
        if counts != (p.db.kv(HEALTH_KEY) or {}):
            p.db.set_kv(HEALTH_KEY, counts or None)


def owned(p: Project, db=None) -> dict[str, dict]:
    """The pauses this project checks: its own, shared ones it set, and the fleet holds it checks
    (fleethold.checker), each once as `machine:<alias>` rather than per resource it covers."""
    from . import fleethold
    db = db or p.db
    out = {k: v for k, v in db.paused_resources().items()
           if not v.get("fleet") and (not v.get("shared") or v.get("project") == p.name)}
    for alias, hold in fleethold.active().items():
        if fleethold.checker(hold) == p.name:
            out[f"{FLEET_PREFIX}{alias}"] = fleethold.entry(hold)
    return out


def _decommissioned(pauses: dict[str, dict]) -> set[str]:
    """The user's pauses in `pauses` of machines the machines list retired (machines.decommissioned_user_pause)."""
    from . import machines
    if not any(v.get("by") == "user" for v in pauses.values()):
        return set()
    known = machines.load()
    return {k for k, v in pauses.items() if machines.decommissioned_user_pause(k, v, known)}


def _user_said(what: str, why: str, state: str, lift: str) -> str:
    """The observation for the user's pause or hold `what`: it stays; their word alone lifts it."""
    if state == "passed":
        return (f"The end condition of the user's {what} passed ({why}). The pause stays: an end condition only "
                f"signals, it never lifts a user's pause. Tell the user once that it passed; never say it is being "
                f"lifted. Lift it only in a turn that carries the user's word ({lift}); do not ask about it.")
    if state == "broken":
        return (f"The end condition of the user's {what} can no longer be checked: {why}. It did not pass. The "
                f"pause stays; only the user's word lifts it ({lift}). Tell the user once that its end condition "
                f"can no longer be checked; never say it passed or is being lifted, and do not ask about it.")
    return (f"The user's {what} waits on a report: {why}. The pause stays; only the user's word lifts it "
            f"({lift}): never say it is being lifted, and do not ask about it.")


def _queue(db, name: str, v: dict, why: str, now: float, state: str = "passed") -> None:
    """Queue the end of pause `name`: a `pause_end_due` event, or for the user's pause one observation.
    `state`: "passed" (its end condition passed, kept in PASSED_KEY), "broken" (its probe is broken or
    the job it waits on failed: it cannot be checked, never shown as passed) or "report" (a report it
    waits on arrived)."""
    if v.get("by") == "user" and state == "passed":
        db.set_kv(PASSED_KEY, {**(db.kv(PASSED_KEY) or {}), name: {"sig": _sig(v), "at": now}})
    if v.get("fleet"):
        _queue_fleet(db, v, why, now, state)
        return
    if v.get("by") == "user":
        db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
             (now, "daemon", "observation", "low",
              _user_said(f"pause of `{name}`", why, state, "`resource_pause` paused false")
              + " It stays listed under Paused resources.", "queued"))
        return
    db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
         (now, "daemon", "pause_end_due", "normal",
          f"The pause of `{name}` ({v.get('reason') or 'no reason given'}) is due to end: {why}. Lift it "
          f"(`resource_pause` paused false) if its cause is over; if it must stay, extend it with "
          f"`resource_pause` paused true, a new `until` or `end_when`, and a `reason` saying why.", "queued"))


def _queue_fleet(db, v: dict, why: str, now: float, state: str = "passed") -> None:
    alias, hid = v.get("machine"), v.get("fleet")
    if v.get("by") == "user":
        db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
             (now, "daemon", "observation", "low",
              _user_said(f"machine-wide hold of `{alias}` (hold {hid})", why, state,
                         f"`resource_pause` machine {alias}, paused false"), "queued"))
        return
    db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
         (now, "daemon", "pause_end_due", "normal",
          f"The machine-wide hold of `{alias}` (hold {hid}, set by project {v.get('project')}: "
          f"{v.get('reason') or 'no reason given'}) is due to end: {why}. Lift it (`resource_pause` with "
          f"`machine` {alias}, paused false) if its cause is over; if it must stay, extend it with "
          f"`resource_pause` machine {alias}, paused true, a new `until` or `end_when`, and a `reason`.", "queued"))


def note_arrived(p: Project, from_project: str | None, now: float | None = None) -> int:
    """A note from `from_project` arrived: re-prompt the coordinator about each of this project's
    pauses that waits on that project's report. Returns how many."""
    if not from_project:
        return 0
    now = time.time() if now is None else now
    n = 0
    for name, v in sorted(owned(p).items()):
        if v.get("report_from") == from_project:
            _queue(p.db, name, v, f"a note from project {from_project}, whose report it waits on, arrived "
                                  f"(the upstream_note event); read it", now, state="report")
            n += 1
    return n


def migrate(p: Project, now: float) -> list[str]:
    """Give each pause without an end DEFAULT_S from now; returns their names."""
    db, done = p.db, []
    until = now + DEFAULT_S

    def add_end(w: dict | None) -> dict | None:
        if w is None or w.get("until") or w.get("end_when"):
            return w
        return {**w, "until": until, "end_default": True}

    with db.tx():
        cur = db.paused_resources(shared=False)
        for name, v in cur.items():
            if isinstance(v, dict) and not (v.get("until") or v.get("end_when")):
                cur[name] = add_end(v)
                done.append(name)
        if done:
            db.set_kv(PAUSED_RESOURCES_KEY, cur)
    for name, v in owned(p).items():
        if v.get("shared") and not (v.get("until") or v.get("end_when")):
            was, new = shared.update_pause(name, add_end)
            if was is not None and new is not was:
                done.append(name)
    if done:
        db.post("out", f"Resource pause{'s' if len(done) > 1 else ''} {', '.join(sorted(done))} had no end; "
                       f"each now ends {ends.shown(until, p)} by default. When that passes the coordinator lifts "
                       f"or extends it; a pause the user set stays until the user lifts it.",
                chat=None, kind="info", severity="low")
    return sorted(done)


class PauseEnds:
    """The daemon's side: default ends for old pauses, end_when probes, and pause_end_due events."""

    def __init__(self, p: Project, log=lambda msg: None):
        self.p, self.log = p, log
        self._checked = 0.0
        self._procs: dict[str, tuple[subprocess.Popen, float, str]] = {}
        self._errs: dict[int, Any] = {}   # id(proc) -> its stderr file
        self._probed: dict[str, float] = {}

    def tick(self, now: float | None = None) -> list[str]:
        """Returns the pauses whose end was reported this time."""
        now = time.time() if now is None else now
        due = self._health_ends(now)
        verdicts = self._reap(now)
        if now - self._checked < CHECK_EVERY_S and not verdicts:
            return due
        self._checked = now
        migrate(self.p, now)
        db = self.p.db
        pauses = owned(self.p)
        gone = _decommissioned(pauses)
        if verdicts:   # each end_when's last verdict: the blocker sweep carries a hold its probe still says not yet
            kept = {k: v for k, v in (db.kv(sweep.PAUSE_PROBES_KEY) or {}).items() if k in pauses}
            kept.update({k: {"probe": rc[1], "rc": rc[0], "at": now} for k, rc in verdicts.items() if k in pauses})
            db.set_kv(sweep.PAUSE_PROBES_KEY, kept or None)
        sent = {k: v for k, v in (db.kv(DUE_KEY) or {}).items() if k in pauses}
        for name, v in sorted(pauses.items()):
            if name in gone:
                continue   # the user's pause of a retired machine: no end to report or probe; it stays as set
            if v.get("user_end") and v.get("by") == "user" and v.get("until") and float(v["until"]) <= now:
                if self._user_end(name, v, now):   # the user's own end: lifted, never reported as due
                    due.append(name)
                continue
            probe, rc = v.get("end_when"), verdicts.get(name, (None, "", ""))
            if isinstance(probe, dict):
                probe = None   # health results come from heal.run, never a shell probe; `until` still ends it
            why, state, mark = "", "passed", _sig(v)
            if v.get("until") and float(v["until"]) <= now:
                why = f"its end time {ends.shown(float(v['until']), self.p)} passed"
            elif probe and ends.always_succeeds(probe):
                # Stored before it was refused: never run. Reported once as broken under its own mark,
                # so its `until` passing is still reported after it.
                state, mark = "broken", _sig(v) + "|always 0"
                why = f"its end_when probe `{probe}` is broken: {ends.TRIVIAL_WHY}"
            elif probe and rc[1] == probe and rc[0] == 0:
                why = f"its end_when probe `{probe}` passed"
            elif probe and rc[1] == probe and detach.failed_check(probe, rc[0]):
                state = "broken"
                why = (f"the detached job its end_when probe `{probe}` waits on ended but failed or left no "
                       f"exit code ({detach.failed_why(probe, rc[0], self.p.root)})")
            elif probe and rc[1] == probe and rc[0] is not None and rc[0] not in ends.NOT_YET_RCS:
                state = "broken"
                why = (f"its end_when probe `{probe}` is broken "
                       f"({rc[0] if isinstance(rc[0], str) else f'exit {rc[0]}'}): it would never end"
                       + (f". Its stderr: {rc[2]}" if rc[2] else ""))
            if why and sent.get(name) != mark:
                _queue(db, name, v, why, now, state)
                sent[name] = mark
                due.append(name)
            elif probe and not why and name not in self._procs and now - self._probed.get(name, 0) >= PROBE_EVERY_S:
                self._start(name, probe, now)
        if sent != (db.kv(DUE_KEY) or {}):
            db.set_kv(DUE_KEY, sent or None)
        passed = db.kv(PASSED_KEY) or {}
        if any(k not in pauses for k in passed):
            db.set_kv(PASSED_KEY, {k: w for k, w in passed.items() if k in pauses} or None)
        if due:
            self.log(f"pause end due: {', '.join(due)}")
        return due

    def _health_ends(self, now: float) -> list[str]:
        from . import heal
        db = self.p.db
        due = self._recover(now)
        counts = db.kv(HEALTH_KEY) or {}
        if not counts:
            return due
        checks = {r["name"]: r for r, _ in heal.checks(db)}
        pauses = owned(self.p)
        gone = _decommissioned(pauses)

        def stale(name: str, count: dict) -> bool:
            end = (pauses.get(name) or {}).get("end_when")
            check = checks.get(end.get("heal")) if isinstance(end, dict) else None
            return check is None or now - count.get("last", 0) > _fresh_s(check)

        if any(stale(k, c) for k, c in counts.items()):
            # Disabled checks and old results cannot prove current health: their window starts over.
            with db.tx():
                saved = db.kv(HEALTH_KEY) or {}
                kept = {k: c for k, c in saved.items() if not stale(k, c)}
                if kept != saved:
                    db.set_kv(HEALTH_KEY, kept or None)
            counts = kept
        for name, v in pauses.items():
            end, count = v.get("end_when"), counts.get(name, {})
            if name in gone or not isinstance(end, dict) or count.get("sig") != _sig(v) \
                    or count.get("n", 0) < end.get("healthy_n", HEALTHY_N) \
                    or count.get("last", 0) - count.get("first", 0) < end.get("clean_s", CLEAN_S):
                continue
            why = (f"heal `{end['heal']}` returned {count['n']} consecutive healthy results "
                   f"over {count['last'] - count['first']:g} s")
            entry = None
            with db.tx():
                # Origin + generation (and the end itself) fence both evidence and removal.
                cur = owned(self.p).get(name)
                if cur is None or _sig(cur) != _sig(v) or (db.kv(HEALTH_KEY) or {}).get(name) != count:
                    continue
                if cur.get("by") == "user":
                    sent = db.kv(DUE_KEY) or {}
                    if sent.get(name) != _sig(v):
                        _queue(db, name, cur, why, now)
                        db.set_kv(DUE_KEY, {**sent, name: _sig(v)})
                        due.append(name)
                    continue
                if cur.get("by") != "coordinator":
                    continue
                entry = {"sig": _sig(cur), "since": cur.get("since"), "origin": cur.get("by"),
                         "project": cur.get("project"), "generation": cur.get("generation"), "why": why, "ts": now}
                if not cur.get("shared"):
                    local = db.paused_resources(shared=False)
                    local.pop(name, None)
                    db.set_kv(PAUSED_RESOURCES_KEY, local)
                    self._ended(name, entry)
            if entry is None:
                continue
            if cur.get("shared"):
                def lift(w: dict | None) -> dict | None:
                    if not (w and _sig(w) == entry["sig"] and w.get("by") == "coordinator"):
                        return w
                    # The receipt is durable before the file goes: a crash after it is recovered by _recover.
                    with db.tx():
                        db.set_kv(RELEASE_KEY, {**(db.kv(RELEASE_KEY) or {}), name: entry})
                    return None
                was, new = shared.update_pause(name, lift)
                if was is None or new is not None:
                    continue
                with db.tx():
                    if name not in (db.kv(RELEASE_KEY) or {}):
                        continue
                    self._ended(name, entry)
            self._after(name)
            due.append(name)
        return due

    def _user_end(self, name: str, v: dict, now: float) -> bool:
        """Lift the user's pause `name` at the end their own words gave (`user_end`), with no model,
        fenced to the pause's identity. A shared pause or fleet hold is journalled first (RELEASE_KEY),
        so a crash between its file and the receipt is finished once by _recover."""
        from . import fleethold
        db = self.p.db
        entry = {"sig": _sig(v), "since": v.get("since"), "origin": v.get("by"), "project": v.get("project"),
                 "generation": v.get("generation"), "ts": now, "kind": USER_END_KIND, "reason": v.get("reason") or "",
                 "why": f"{USER_END_WHY} ({ends.shown(float(v['until']), self.p)})",
                 **({"fleet": v["fleet"], "machine": v.get("machine")} if v.get("fleet") else {})}
        if not (v.get("fleet") or v.get("shared")):
            with db.tx():
                local = db.paused_resources(shared=False)
                if name not in local or _sig(local[name]) != entry["sig"]:
                    return False
                local.pop(name)
                db.set_kv(PAUSED_RESOURCES_KEY, local)
                self._ended(name, entry)
            self._after(name, entry)
            self.log(f"pause of {name} {entry['why']}: lifted")
            return True
        with db.tx():
            db.set_kv(RELEASE_KEY, {**(db.kv(RELEASE_KEY) or {}), name: entry})
        if v.get("fleet"):
            fleethold.lift_at_user_end(str(v.get("machine")), v.get("generation"), self.p.name, now)
        else:
            shared.update_pause(name, lambda w: None if w and _sig(w) == entry["sig"] else w)
        return name in self._recover(now)

    def _ended(self, name: str, entry: dict) -> None:
        """The receipt of one release, inside the caller's transaction: its audit event and one sweep
        signal, its evidence and journal entry cleared, and its waiters made due (a fleet hold's by
        sync_shared_pauses, in _after). A `user_end` lift's event is USER_END_KIND, for the coordinator."""
        from . import coordinator as coord
        db = self.p.db
        journal = db.kv(RELEASE_KEY) or {}
        if name in journal:
            journal.pop(name)
            db.set_kv(RELEASE_KEY, journal or None)
        seen = db.kv(SHARED_SEEN_KEY) or {}
        if name in seen and (seen[name] or {}).get("since") == entry.get("since"):
            seen.pop(name)
            db.set_kv(SHARED_SEEN_KEY, seen)
        saved = db.kv(HEALTH_KEY) or {}
        if saved.get(name, {}).get("sig") == entry["sig"]:
            saved.pop(name)
            db.set_kv(HEALTH_KEY, saved or None)
        if not entry.get("fleet") and name not in db.paused_resources():
            coord._wake_pause_waiters(db, name, float(entry.get("since") or 0))
        kind = entry.get("kind") or "pause_ended"
        what = (f"machine-wide hold of `{entry.get('machine')}` (hold {entry['fleet']})" if entry.get("fleet")
                else f"pause of `{name}`")
        db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
             (entry["ts"], "daemon", kind, "normal" if kind == USER_END_KIND else "low",
              f"The user's {what} ({entry.get('reason') or 'no reason given'}) {entry['why']}: the daemon lifted "
              f"it and the tasks that waited on it are due. Submit the work it held; never ask the user to lift it."
              if kind == USER_END_KIND else f"Pause ended: {name}; daemon lifted it because {entry['why']}.",
              json.dumps({"resource": name, "by": "daemon", "origin": entry.get("origin"),
                          "project": entry.get("project"), "generation": entry.get("generation"),
                          "why": entry["why"]}), "queued"))
        sweep.signal(db, f"pause ended: {name}", rearm=True)

    def _after(self, name: str, entry: dict | None = None) -> None:
        from . import coordinator as coord
        if (entry or {}).get("fleet"):
            coord.sync_shared_pauses(self.p)   # tells this project's workers and wakes its waiters
        elif name not in self.p.db.paused_resources():
            coord._tell_runs(self.p.db, name, False)

    def _recover(self, now: float) -> list[str]:
        """Finish shared releases a crash cut short. A file that still holds the pause was never
        removed: the entry goes and the pause stays. One replaced in place (same `since`) was not
        removed by us either. Otherwise the removal happened: its receipt is written once."""
        db, done = self.p.db, []
        from . import fleethold
        for name, entry in sorted((db.kv(RELEASE_KEY) or {}).items()):
            if entry.get("fleet"):
                hold = fleethold.active().get(str(entry.get("machine")))
                w = fleethold.entry(hold) if hold else None
            else:
                w = shared.read_pause(name)
            released = w is None or (_sig(w) != entry.get("sig") and w.get("since") != entry.get("since"))
            with db.tx():
                journal = db.kv(RELEASE_KEY) or {}
                if journal.get(name) != entry:
                    continue
                if released:
                    self._ended(name, entry)
                else:
                    journal.pop(name)
                    db.set_kv(RELEASE_KEY, journal or None)
            if released:
                self._after(name, entry)
                done.append(name)
        if done:
            self.log(f"pause release recovered: {', '.join(done)}")
        return done

    def _start(self, key: str, probe: str, now: float) -> None:
        self._probed[key] = now
        try:
            self._procs[key] = (ends.spawn_probe(self.p, probe, self._errs), now, probe)
        except OSError as e:
            self.log(f"end_when probe of the pause of {key} could not start: {e}")

    def _reap(self, now: float) -> dict[str, tuple[Any, str, str]]:
        return ends.reap_probes(self._procs, self._errs, now, self.p)

    def stop(self) -> None:
        ends.stop_probes(self._procs, self._errs)


class MuteEnds:
    """The daemon's side of observation mutes with an `end_when` probe (screen.mute): the same probe
    semantics as a pause's. Its pass ends the mute with its summary event, with no model; a broken or
    timed-out probe is reported once and the mute keeps its time."""

    def __init__(self, p: Project, log=lambda msg: None):
        self.p, self.log = p, log
        self._checked = 0.0
        self._procs: dict[str, tuple[subprocess.Popen, float, str]] = {}
        self._errs: dict[int, Any] = {}
        self._probed: dict[str, float] = {}

    def tick(self, now: float | None = None) -> list[str]:
        """Returns the mutes ("source 'match'") their probe ended this time."""
        from . import screen as scr
        now = time.time() if now is None else now
        verdicts = ends.reap_probes(self._procs, self._errs, now, self.p)
        if now - self._checked < CHECK_EVERY_S and not verdicts:
            return []
        self._checked = now
        done = []
        for m in scr.mutes(self.p.db, now):
            probe = m.get("end_when")
            if not probe:
                continue
            if ends.always_succeeds(probe):   # stored before it was refused: never run; its time ends the mute
                scr.mute_probe_broken(self.p.db, m["source"], m["match"], probe, ends.TRIVIAL_WHY, now, where=self.p)
                continue
            key = f"{m['source'].lower()}\n{m['match'].lower()}"
            rc, ran, err = verdicts.get(key, (None, "", ""))
            if ran == probe and (rc == 0 or detach.failed_check(probe, rc)):
                why = (f"its end_when probe `{probe}` passed" if rc == 0 else
                       f"the detached job its end_when probe `{probe}` waits on ended but failed or left no "
                       f"exit code ({detach.failed_why(probe, rc, self.p.root)})")
                if scr.end_mute(self.p.db, m["source"], m["match"], probe, why, now, where=self.p):
                    done.append(f"{m['source']} {m['match']!r}")
                continue
            if ran == probe and rc is not None and rc not in ends.NOT_YET_RCS:
                scr.mute_probe_broken(self.p.db, m["source"], m["match"], probe,
                                      (rc if isinstance(rc, str) else f"exit {rc}") + (f"; stderr: {err}" if err else ""),
                                      now, where=self.p)
            if key not in self._procs and now - self._probed.get(key, 0) >= PROBE_EVERY_S:
                self._probed[key] = now
                try:
                    self._procs[key] = (ends.spawn_probe(self.p, probe, self._errs), now, probe)
                except OSError as e:
                    self.log(f"end_when probe of the mute of {m['source']} {m['match']!r} could not start: {e}")
        if done:
            self.log(f"mute ended by its probe: {', '.join(done)}")
        return done

    def stop(self) -> None:
        ends.stop_probes(self._procs, self._errs)
