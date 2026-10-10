# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""End conditions on resource pauses.

A resource pause (`resource_pause`, `ttp pause --resource`) must say when it ends: `until` (a time
at most MAX_UNTIL_S ahead) and/or `end_when` (a read-only shell probe like `retry_when`: exit 0 =
over; 1, 75 or 255 = not yet). It may also name `report_from`, another project whose report it
waits on. A pause never ends by itself: when its time passes, its probe passes or breaks, the
daemon queues one `pause_end_due` event and the coordinator lifts the pause or extends it with a
reason. A note from the `report_from` project re-prompts it the same way. A pause the user set is
lifted only on their word: its end gives one low, ask-free line instead. A pause recorded without
an end (from before ends were required) gets DEFAULT_S and one info message saying so.

Only the project that set a shared pause (shared.py) checks it, so one owner is prompted."""
from __future__ import annotations

import re
import subprocess
import time
from typing import Any

from . import ends, shared
from .db import PAUSED_RESOURCES_KEY
from .project import Project

MAX_UNTIL_S = 7 * 86400
DEFAULT_S = 7 * 86400
END_FIELDS = ("until", "end_when", "report_from", "end_default")
DUE_KEY = "pause_end_due"      # kv: {resource: end signature} whose end was reported to the coordinator
CHECK_EVERY_S = 60
PROBE_EVERY_S = ends.PROBE_EVERY_S
PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,79}")
NEEDS_END = ("a resource pause needs an end: `until` (a delay such as 2d or an ISO time, at most 7 days "
             "ahead) or `end_when` (a read-only shell probe that exits 0 once the pause can end)")


def from_action(a: dict, now: float | None = None) -> dict:
    """The end a pause action sets: {"until": ts, "end_when": probe, "report_from": project}, only
    those given. Raises ValueError when it has neither `until` nor `end_when`."""
    now = time.time() if now is None else now
    out: dict = {}
    if a.get("until"):
        out["until"] = ends.parse_expires(a["until"], now, what="until", max_s=MAX_UNTIL_S)
    probe = a.get("end_when")
    if probe:
        if not isinstance(probe, str) or "\n" in probe.strip() or len(probe) > ends.PROBE_CHARS:
            raise ValueError(f"end_when must be one shell command on one line, at most {ends.PROBE_CHARS} characters")
        out["end_when"] = probe.strip()
    who = str(a.get("report_from") or "").strip()
    if who:
        if not PROJECT_RE.fullmatch(who):
            raise ValueError(f"report_from {who!r} is not a project name")
        out["report_from"] = who
    if not (out.get("until") or out.get("end_when")):
        raise ValueError(NEEDS_END)
    return out


def describe(v: dict) -> str:
    """'ends 2026-10-06T12:00Z, or when `probe` passes; waits on project x's report' (or '')."""
    parts = ([ends.stamp(float(v["until"]))] if v.get("until") else []) \
        + ([f"when `{v['end_when']}` passes"] if v.get("end_when") else [])
    out = "ends " + ", or ".join(parts) if parts else ""
    if v.get("end_default"):
        out += " (a default: it was set without an end)"
    if v.get("report_from"):
        out += f"; waits on project {v['report_from']}'s report"
    return out.strip("; ")


def _sig(v: dict) -> str:
    return f"{v.get('since')}|{v.get('until')}|{v.get('end_when')}"


def owned(p: Project, db=None) -> dict[str, dict]:
    """The pauses this project checks: its own, and shared ones it set."""
    db = db or p.db
    return {k: v for k, v in db.paused_resources().items() if not v.get("shared") or v.get("project") == p.name}


def _queue(db, name: str, v: dict, why: str, now: float) -> None:
    if v.get("by") == "user":
        # The user's pause: their word lifts it, so the coordinator neither lifts it nor asks.
        db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
             (now, "daemon", "observation", "low",
              f"The user's pause of `{name}` reached its end ({why}). Only the user lifts it: leave it, do not "
              f"ask about it; it stays listed under Paused resources.", "queued"))
        return
    db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
         (now, "daemon", "pause_end_due", "normal",
          f"The pause of `{name}` ({v.get('reason') or 'no reason given'}) is due to end: {why}. Lift it "
          f"(`resource_pause` paused false) if its cause is over; if it must stay, extend it with "
          f"`resource_pause` paused true, a new `until` or `end_when`, and a `reason` saying why.", "queued"))


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
                                  f"(the upstream_note event); read it", now)
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
                       f"each now ends {ends.stamp(until)} by default. When that passes the coordinator lifts "
                       f"or extends it; a pause the user set stays until the user lifts it.",
                chat=None, kind="info", severity="low")
    return sorted(done)


class PauseEnds:
    """The daemon's side: default ends for old pauses, end_when probes, and pause_end_due events."""

    def __init__(self, p: Project, log=lambda msg: None):
        self.p, self.log = p, log
        self._checked = 0.0
        self._procs: dict[str, tuple[subprocess.Popen, float, str]] = {}
        self._probed: dict[str, float] = {}

    def tick(self, now: float | None = None) -> list[str]:
        """Returns the pauses whose end was reported this time."""
        now = time.time() if now is None else now
        verdicts = self._reap(now)
        if now - self._checked < CHECK_EVERY_S and not verdicts:
            return []
        self._checked = now
        migrate(self.p, now)
        db = self.p.db
        pauses = owned(self.p)
        sent = {k: v for k, v in (db.kv(DUE_KEY) or {}).items() if k in pauses}
        due = []
        for name, v in sorted(pauses.items()):
            probe, rc = v.get("end_when"), verdicts.get(name, (None, ""))
            why = ""
            if v.get("until") and float(v["until"]) <= now:
                why = f"its end time {ends.stamp(float(v['until']))} passed"
            elif probe and rc[1] == probe and rc[0] == 0:
                why = f"its end_when probe `{probe}` passed"
            elif probe and rc[1] == probe and rc[0] is not None and rc[0] not in ends.NOT_YET_RCS:
                why = (f"its end_when probe `{probe}` is broken "
                       f"({rc[0] if isinstance(rc[0], str) else f'exit {rc[0]}'}): it would never end")
            if why and sent.get(name) != _sig(v):
                _queue(db, name, v, why, now)
                sent[name] = _sig(v)
                due.append(name)
            elif probe and not why and name not in self._procs and now - self._probed.get(name, 0) >= PROBE_EVERY_S:
                self._start(name, probe, now)
        if sent != (db.kv(DUE_KEY) or {}):
            db.set_kv(DUE_KEY, sent or None)
        if due:
            self.log(f"pause end due: {', '.join(due)}")
        return due

    def _start(self, key: str, probe: str, now: float) -> None:
        self._probed[key] = now
        try:
            proc = subprocess.Popen(probe, shell=True, cwd=str(self.p.root), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.log(f"end_when probe of the pause of {key} could not start: {e}")
            return
        self._procs[key] = (proc, now, probe)

    def _reap(self, now: float) -> dict[str, tuple[Any, str]]:
        out = {}
        for key, (proc, started, probe) in list(self._procs.items()):
            rc = proc.poll()
            if rc is None and now - started < ends.PROBE_TIMEOUT_S:
                continue
            del self._procs[key]
            if rc is None:
                ends._kill(proc)
            out[key] = ("timeout" if rc is None else rc, probe)
        return out

    def stop(self) -> None:
        for proc, _, _ in self._procs.values():
            ends._kill(proc)
        self._procs.clear()
