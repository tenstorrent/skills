# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared machines: one recovery owner per machine, and a change ledger every co-tenant sees.

Several projects of one user may use the same machine (each lists it in its charter's Resources: its
co-tenants). Two things keep them from working against each other:

Recovery owner. machines.json gives a machine a `recovery_owner` project and a `recovery_fallback`
(`ttp machines set <alias> --owner <project> [--fallback <project>]`). A co-tenant that sees the machine
down or held past its grace (a command watcher's observation with `machine` and `condition`, or `report`)
records the condition in the shared routes file; the first daemon to tick routes it, model-free, to the
owner: a high-severity note in its inbox (`upstream.send`), or an event when the owner is the daemon's own
project, which the coordinator takes as a tricky turn. When the owner cannot act (its daemon stopped or
stale, the project paused, its coordinator logged out or failing) for UNAVAILABLE_S, the condition goes to
the fallback, then to the user as an outage. Before each page, the ledger reads the owner's (then the
fallback's) own heal checks naming the machine, read-only (responsibilities.healthy_checks): while one that
observes the condition ran healthy and fresh (any such check for `down`, only a broker check for `held`)
and that project does not itself report the condition, the page is held, and that project gets one low
note per episode naming the reporting projects (for `down`, likely their own route problem).
A condition no co-tenant reported for CONDITION_GONE_S, or reported cleared, is over. With no owner set nothing is routed: each co-tenant's digest names the gap once.

Quiet outages (quiet_reason). Nobody is paged about a condition the harness already knows: a known outage
recorded with `ttp machines outage <alias> "<why>" [--owner <who>] [--covers <parts>] --expires <t>` (a
hardware repair, often owned by a human or an external ticket; `covers`, optional, lists the parts it explains
from OUTAGE_COVERS, or services on the machine by name, for co-tenant watchers and the harness's own heal checks,
which it then quiets only for results it explains (heal.outage_explains)), or an open change that expects the condition (`--expect down`).
Known outages are kept per machine and project: each project's entry is set again, expires and is cleared
on its own (`--clear` ends only the caller's, or `--by <project>`'s), and the machine counts as out while
any entry is open; status, the daily review and paging list every entry with its project, owner and end.
The owner's first page still goes, but nothing escalates to the fallback or the user, while: the user paused
a resource on the machine, a reporting heal check carries a `known_fault`, or no open task of any project on
this machine uses it (a task's resource on it that its project paused does not count). A reporter this
machine cannot read counts as needing it. Each such outage is recorded once per machine (`quiet`), for the
daily review (review_lines), never alerted; its user alert clears. Once the reason ends, the steps resume
from that moment.

Change ledger. Every live change to a shared machine (a drop-in, a disabled recovery switch, a pause) is
recorded with `ttp machines change add <alias> "<what>" --undo "<cmd>" (--expires <t> | --until-probe
"<cmd>" | --until-task <id>)`, or an until-probe with an --expires (it ends at whichever comes first): who made it (the project), why (the text), how to undo it and when it ends. Each co-tenant's
digest lists the open entries for machines in its Resources, one line each. An entry is overdue when it
expires, its until-probe passes (exit 0: what it waited for is over, so the undo is due; add and edit refuse
a probe that always exits 0, ends.always_succeeds, and Probes reports an older one instead of running it; add
and edit also run a new probe once, check_probe_unmet, and refuse one that passes already, unless --allow-met-probe) or its until-task
(a task of the project that made it, such as the follow-up that uses a staged build tree) is done, failed,
cancelled or gone; a probe-only or task-only entry also expires after PROBE_END_S, and a continuation task
re-extends it with `ttp machines change edit <id> --until-task <its own id>`. The daily review lists the
project's open entries whose task is over (task_lines). An overdue entry pages the project that made it, then the machine's
owner, then its fallback, each after the one before cannot act for UNAVAILABLE_S or let REPAGE_S pass,
then the user. Nothing is undone automatically unless asked (Auto-undo): whoever is paged runs the undo and closes the entry
(`ttp machines change close <id>`). An entry whose undo is a no-op (noop_undo: empty, `true`, `:`, or
`none` as a word, as in `none: <why>`) is never paged: once its until-probe passed PROBE_PASSES times in a row
(not one ends.always_succeeds refuses, nor a pass whose job failed), its until-task is done, failed or cancelled
(task_status; a deleted or unknown task, or one no daemon marks, pages as before) or, with no until-probe or
until-task, its time passed, the first daemon's route closes it under the lock, model-free and once (auto_close_rule), with
`auto_closed` and closed_by naming the rule. Probes run only in the daemon of the project that made the entry,
from its root, as end probes do, starting PROBE_GRACE_S after the entry; a probe counts as passed only after
PROBE_PASSES passes in a row (each logged with its output's tail), keeps running while the entry is open, and a
later "not yet" (1 or 75) clears the pass and its pages; a broken one (an exit code other than 0, 1, 75 or ssh's own 255, or a
timeout) queues one low `change_probe_broken` event per kind of breakage; the same daemon reads its until-tasks'
states. `ttp machines change edit <id> [--what] [--undo] [--expires] [--until-probe | --until-task]`
corrects an open entry in place (same id), with an edit trail in the entry; giving its until-probe again, even
the same one, drops its result and passes so far: it is judged afresh after PROBE_GRACE_S.

Auto-undo. `ttp machines change add ... --auto-undo` (or `edit <id> --auto-undo`; `--no-auto-undo` turns it
off) asks the recording project's daemon to run the undo itself, model-free, once the entry is due as the
auto-close judges it (_due: its until-probe passed PROBE_PASSES times in a row, or its --expires passed with no
until-probe). It fits a quick, idempotent undo that is safe to run unattended (remove a scratch dir, clear a
hold). Add and edit refuse it (check_auto_undo) for a no-op undo (the auto-close covers it), an undo killscan
flags (a kill by name or pattern), an until-probe that always exits 0, and an --until-task end. Before each run
the daemon records a claim under the lock (try, time, its pid); the undo then runs from the project's root with
stdin from /dev/null, in a session and process group of its own, and is killed with its whole group after
AUTO_UNDO_TIMEOUT_S. Exit 0 closes the entry ("auto-undo ok", closed_by `auto-undo`); 1, 75 or 255 (not yet,
in use, host unreachable) retries after PROBE_GRACE_S, up to AUTO_UNDO_TRIES runs; any other exit, a timeout,
or a claim no running daemon holds (a restart interrupted the run: it is never run again) gives up
(`auto_undo_failed`, quoting the claim), and route then pages as for any overdue entry, with the output's tail.
Until then route pages nobody while that project can act and the entry stays due: an until-probe that says "not
yet" again ends the episode (a later pass starts afresh) and route pages it as any overdue entry. Every run, with its last AUTO_UNDO_TAIL_CHARS of
output, is kept in `auto_undo_runs` (`ttp machines change list --all`, `--json`). While the machine is under a
known outage, held machine-wide, or a resource on it is paused (the project's own pause or a shared one), the
undo waits, unrun and unpaged, and runs once that ends.

Test changes. `ttp machines change add ... --test` marks a change made for a test: `ttp ttr` labels the
recording project's incidents on that machine that start inside its window as tests (ttr.test_windows).
`--since <t>` backdates a new entry's start (`since`; its `added` stays when it was recorded): a delay back
(`2h`) or an ISO time, in the project's home zone unless it names one, not in the future and at most
SINCE_MAX_S back. `ttp machines change edit <id> --test` (`--no-test` undoes it) flags an entry, open or
closed, after the fact (set_test), so incidents already recorded inside its window are relabelled at the next
report; only the project that recorded it or the machine's recovery owner may, and the edit trail keeps when,
who and the old and new value. Unlike other edits, a flag edit is queued from a read-only sandbox.

Planned power cycles. `ttp machines change add <alias> "<why>" --power-cycle-at <t>` records a power-cycle
intent: an entry of kind `power_cycle` that needs no undo and closes itself POWER_CYCLE_AFTER_S after T
(never overdue; any co-tenant daemon closes it once past). It expects the machine `down` only from
POWER_CYCLE_LEAD_S before T until it ends (expects_now), so it never hides an outage outside the cycle. From POWER_CYCLE_LEAD_S before T until as long after it, each
daemon tells its running workers on that machine (the daemon's own host, or a task resource on it) once,
through their steer.md, to commit and hand off now (daemon.power_cycle_notices). An intent `ttp drain
--reboot` writes carries `hold_from`, when it was written: its window starts there when that is earlier
(cycle_window), so the drain is held and steered from its start (hostcycle); its expects still start
POWER_CYCLE_LEAD_S before T, which the drain moves to the cycle once it records it.

Both files live next to machines.json, written whole under one lock, so co-tenant daemons and CLIs never
lose each other's writes. Other projects' state is only read (their database, read-only), never written.

Queued requests. A worker whose sandbox keeps the user's tt-project home read-only (it may write only its
project's state, as a fenced Codex worker) cannot take the lock. Its `ttp machines change add` or `close`
then writes the checked request to REQUESTS under the project's state instead, named by a hash of its
content (request_id), and the project's own daemon records it on its next routing tick (apply_requests),
model-free. A retry of the same command finds the request already queued, or its open entry (which keeps
the request's id as `request`), and adds nothing; a daemon killed between the ledger write and the request's
removal finds that entry and only removes the request. A request the ledger refuses by then (its machine
gone) is kept as `<id>.failed.json` with the reason, and logged. `ttp machines change edit` is not queued
(close the entry and add a new one), except its --test or --no-test (op `test`).

A page is never lost nor sent twice: `route` records each due page as pending, with a text fixed for its
step, and hands it to one daemon at a time (a claim that lapses after CLAIM_S); that daemon marks it
`delivered` only once it went out, or `release`s it for the next tick. A crash in between leaves it
pending, and the resend is idempotent (same inbox text, same event fingerprint, same alert episode).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import detach, ends, machines, netwait, outagetask, pagesev, project
from .db import TERMINAL_TASK_STATES

LEDGER = "machine-changes.json"
ROUTES = "machine-routes.json"
LOCK = "machine-ledger.lock"
REQUESTS = "machine-requests"   # under a project's state: ledger writes a sandboxed worker queued (apply_requests)
UNWRITABLE = (errno.EROFS, errno.EACCES, errno.EPERM)   # the shared ledger is fenced off, not broken
CLAIM_S = 120               # a pending page one daemon took and did not mark delivered is retried after this
UNAVAILABLE_S = 1800         # a target that cannot act this long is passed over
REPAGE_S = 86400             # an item still open this long after its page is paged again, one step on
GRACE_S = 1800               # default: how long a condition lasts before it is routed
CONDITION_GONE_S = 3 * 3600  # a condition nobody reported this long is over
PROBE_END_S = 7 * 86400      # an entry with only an until-probe or until-task is overdue after this anyway
PROBE_GRACE_S = 300          # an until-probe first runs this long after its entry (or its new probe) was made
PROBE_PASSES = 2             # passes in a row before an until-probe counts as passed
ADD_PROBE_TIMEOUT_S = 10     # how long `change add` and `edit` wait for its until-probe's first run (check_probe_unmet)
CLOSED_KEPT_S = 30 * 86400   # closed entries kept this long
SINCE_MAX_S = 7 * 86400      # `change add --since` backdates a start at most this far
TEXT_CHARS = 300
CONDITIONS = ("down", "held")
# what a known outage explains (`ttp machines outage --covers`), besides a service's name; none given:
# the whole machine
OUTAGE_COVERS = ("host", "power", "network", "tray", "chip", "fabric")
COVER_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")   # a service a known outage names
OUTAGE_ENTRIES = "outage_entries"   # routes-file key: {alias: {project: known outage entry}}
QUIET_KEPT_S = 2 * 86400     # a quiet outage's daily-review record is kept this long after it was last seen
REVIEW_S = 86400             # the daily review lists the quiet outages seen within this
GAP_KV = "machine_owner_gap_named"
POWER_CYCLE = "power_cycle"
POWER_CYCLE_LEAD_S = 600     # running workers on the machine are told to hand off this long before the cycle
POWER_CYCLE_AFTER_S = 3600   # a power-cycle intent closes itself this long after its time
USER = "the user"
# The coordinator digest shows each change on one line of about DIGEST_LINE_CHARS (change_line's clip): a
# long reason, undo or probe would otherwise fill the digest, which every turn writes to the cache again.
DIGEST_LINE_CHARS = 300
CLIP_PART_CHARS = 80   # an undo, until-probe or overdue cause in a clipped line
CLIP_WHAT_MIN = 60     # the reason keeps at least this much


def _path(name: str) -> Path:
    return project.HOME_DIR / name


@contextlib.contextmanager
def _locked():
    """The one lock both files are read and written under."""
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(_path(LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(name: str, empty: dict) -> dict:
    try:
        doc = json.loads(_path(name).read_text())
    except (OSError, ValueError):
        return json.loads(json.dumps(empty))
    return doc if isinstance(doc, dict) else json.loads(json.dumps(empty))


def _write(name: str, doc: dict) -> None:
    project.write_json(_path(name), doc, 0o600)


def _one_line(text: Any, chars: int = TEXT_CHARS) -> str:
    return " ".join(str(text or "").split())[:chars]


# ledger -------------------------------------------------------------------------------------------

def add_change(alias: str, what: str, undo: str, by: str, expires: Any = None, until_probe: str | None = None,
               task: int | None = None, now: float | None = None, expects=None, extra: dict | None = None,
               until_task: int | None = None, queue: Path | None = None, test: bool = False,
               since: float | None = None, where=None, auto_undo: bool = False, project=None,
               allow_met_probe: bool = False, warn: Callable[[str], None] | None = None) -> dict:
    """Record a live change to machine `alias` made by project `by`; returns the entry. `queue`: the project's
    state dir, where the change is queued for its daemon when the shared ledger is read-only here (returns
    the request then, with `queued`: its id; see Queued requests). `expects`: the
    event fingerprints or machine conditions (down, held) the change is known to cause, which then
    keep the coordinator's turn routine (coordinator._known_case). `extra`: more fields (a kind).
    `until_task`: a task of project `by` whose end (done, failed, cancelled, gone) makes it overdue
    (check_task validates it). `test`: the change is made for a test, so `ttp ttr` labels this project's
    incidents on `alias` that start while it is open as tests (ttr.test_windows). `since`: an epoch time the
    change started before it was recorded (parse_since): its window starts there. `where`: the zone a
    zone-less --expires is read in (ends.parse_expires). `auto_undo`: the recording project's daemon runs the
    undo itself once the entry is due (module doc: Auto-undo; check_auto_undo refuses unsafe ones). `project`
    (a Project): run the until-probe once first, as its daemon would (check_probe_unmet; `allow_met_probe`,
    `warn`)."""
    now = time.time() if now is None else now
    if since is not None and not now - SINCE_MAX_S <= float(since) <= now:
        raise ValueError(f"--since must lie within the last {SINCE_MAX_S / 86400:g} days, not in the future")
    if alias not in machines.load():
        raise ValueError(f"no machine {alias!r} in `ttp machines list`")
    what, undo, probe = _one_line(what), _one_line(undo, 1000), _one_line(until_probe, ends.PROBE_CHARS)
    if not what or not undo:
        raise ValueError("say what changed and give the --undo command")
    if probe and until_task:
        raise ValueError("give one end: --until-probe or --until-task")
    if not expires and not probe and not until_task:
        raise ValueError(END_HINT)
    if probe:
        ends.check_sh(probe, "--until-probe")
        ends.check_not_trivial(probe, "--until-probe")
    if auto_undo:
        check_auto_undo(undo, probe, until_task)
    if probe and project is not None:
        check_probe_unmet(project, probe, allow_met_probe, warn)
    end = float(expires) if isinstance(expires, (int, float)) else \
        ends.parse_expires(expires, now, where=where) if expires else now + PROBE_END_S   # a number: an epoch time
    fields = {"alias": alias, "what": what, "undo": undo, "by": by, "task": task, "added": now, "expires": end,
              "probe": probe or None, **({"until_task": int(until_task)} if until_task else {}),
              **({"test": True} if test else {}), **({"since": float(since)} if since is not None else {}),
              **({"auto_undo": True} if auto_undo else {}), **(extra or {})}
    if expects:
        fields["expects"] = [_one_line(x, 200) for x in expects if _one_line(x, 200)]
    try:
        return _store_change(fields)
    except OSError as e:
        if queue is None or e.errno not in UNWRITABLE:
            raise
        return _queue(queue, "add", fields)


def check_probe_unmet(p, probe: str, allow_met: bool = False, warn: Callable[[str], None] | None = None) -> None:
    """`change add` and `edit`: run until-probe `probe` once in project `p`'s context, as Probes does (ends.probe_once),
    for at most ADD_PROBE_TIMEOUT_S. Exit 0 means the change is over already, so the entry would be overdue at
    once: raises ValueError, unless `allow_met` (then a warning). A timeout, a probe that cannot run or one
    that exits with a code other than "not yet" (ends.NOT_YET_RCS) is recorded anyway, with a warning naming
    it; a 255 too, since only the daemon tells an unreachable host from a broken probe."""
    warn = warn or (lambda msg: print(msg, file=sys.stderr))
    rc, out = ends.probe_once(p, probe, ADD_PROBE_TIMEOUT_S)
    tail = f": {_one_line(out, 200)}" if out.strip() else ""
    if rc == 0:
        if not allow_met:
            raise ValueError(f"the --until-probe already passes (exit 0 now), so the entry would be overdue at "
                             f"once: give a probe that stays false until the change should end, or use --expires "
                             f"(--allow-met-probe records it anyway)")
        warn(f"warning: the --until-probe already passes (exit 0 now): the entry is overdue at once "
             f"(recorded, --allow-met-probe){tail}")
    elif rc == "timeout":
        warn(f"warning: the --until-probe did not finish within {ADD_PROBE_TIMEOUT_S}s (killed): recorded; the "
             f"daemon gives it {ends.PROBE_TIMEOUT_S}s, counts a timeout as not yet and reports it broken if it "
             f"keeps timing out there")
    elif isinstance(rc, str):
        warn(f"warning: the --until-probe {rc}: recorded; the daemon reports it broken until it runs")
    elif rc == 255:
        warn(f"warning: the --until-probe exits 255 now (ssh could not reach a host, or the probe is broken): "
             f"recorded; the daemon tells which{tail}")
    elif rc not in ends.NOT_YET_RCS and detach.failed_check(probe, rc):
        warn(f"warning: the detached job the --until-probe waits on ended but failed (exit {rc} now): recorded, "
             f"but the entry is overdue at once{tail}")
    elif rc not in ends.NOT_YET_RCS:
        warn(f"warning: the --until-probe exits {rc} now, which the daemon treats as a broken probe (only 0 = "
             f"over, 1 or 75 = not yet): recorded{tail}")


def parse_since(raw: str, now: float | None = None, where=None) -> float:
    """`change add --since`: a delay back (`2h`, `3d`) or an ISO date or time, in the zone of `where` (a
    Project, settings or zone name; None: timefmt.ambient()) unless it names one. Refuses a time ahead of now or more than
    SINCE_MAX_S back."""
    now = time.time() if now is None else now
    s = str(raw or "").strip()
    m = re.fullmatch(r"([0-9]+) ?([smhdw])", s)
    if m:
        at = now - int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}[m[2]]
    else:
        iso = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", s.replace(" ", "T", 1).replace("Z", "+00:00"))
        try:
            d = datetime.fromisoformat(iso)
        except ValueError:
            raise ValueError(f"--since {raw!r}: use a delay back such as 2h, or an ISO time such as "
                             f"2026-10-05T09:00") from None
        from . import timefmt
        at = (d if d.tzinfo else d.replace(tzinfo=timefmt.reading_zone(where))).timestamp()
    if at > now:
        raise ValueError(f"--since {raw!r} is in the future: a change's start can only be backdated")
    if at < now - SINCE_MAX_S:
        raise ValueError(f"--since {raw!r} is more than {SINCE_MAX_S / 86400:g} days back")
    return at


def _store_change(fields: dict, request: str | None = None) -> dict:
    """Append entry `fields` under the next id; with `request`, only once: an open entry already made from
    that request is returned instead."""
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        if request:
            done = next((c for c in doc.get("changes", []) if isinstance(c, dict) and c.get("request") == request
                         and not c.get("closed")), None)
            if done:
                return done
        n = int(doc.get("next") or 1)
        entry = {"id": n, **fields, **({"request": request} if request else {})}
        doc["next"] = n + 1
        doc["changes"] = [c for c in doc.get("changes", []) if not c.get("closed")
                          or float(fields["added"]) - float(c["closed"]) < CLOSED_KEPT_S] + [entry]
        _write(LEDGER, doc)
    return entry


def add_power_cycle(alias: str, at: Any, by: str, why: str = "", task: int | None = None,
                    now: float | None = None, extra: dict | None = None, queue: Path | None = None,
                    where=None) -> dict:
    """Record a planned power cycle of `alias` at `at` (a delay or a time, as --expires takes, read in `where`'s zone).
    `extra`: more fields (`ttp drain --reboot` adds the boot it was made on and its reason)."""
    now = time.time() if now is None else now
    when = ends.parse_expires(at, now, what="--power-cycle-at", where=where)
    what = f"power-cycle intent, at {ends.shown(when, where)}" + (f": {_one_line(why)}" if _one_line(why) else "")
    return add_change(alias, what, "none: a planned power cycle; the entry closes itself after it", by,
                      expires=when + POWER_CYCLE_AFTER_S, task=task, now=now, expects=["down"],
                      extra={**(extra or {}), "kind": POWER_CYCLE, "at": when}, queue=queue, where=where)


def cycle_window(c: dict) -> tuple[float, float]:
    """(start, end) of power-cycle intent c's window: POWER_CYCLE_LEAD_S either side of its time, starting
    at its `hold_from` instead when that is earlier (a drain's intent holds from when it was written)."""
    at = float(c.get("at") or 0)
    start = at - POWER_CYCLE_LEAD_S
    if isinstance(c.get("hold_from"), (int, float)):
        start = min(start, float(c["hold_from"]))
    return start, at + POWER_CYCLE_LEAD_S


def power_cycles_due(now: float | None = None) -> list[dict]:
    """Open power-cycle intents whose window (cycle_window) holds now."""
    now = time.time() if now is None else now
    return [c for c in changes() if c.get("kind") == POWER_CYCLE
            and cycle_window(c)[0] <= now <= cycle_window(c)[1]]


def retime_power_cycle(cid: int, at: float, by: str, now: float | None = None,
                       hold_from: float | None = None, drain: float | None = None) -> dict:
    """Move open power-cycle intent #cid to epoch time `at` (its end follows), with an edit trail: a drain
    that runs longer pushes it on, and the cycle itself, once recorded, brings it to now. `hold_from`: its
    window also starts no later than that (and no later than it did), in the same write, so a drain that
    takes up an intent and pushes it on keeps holding from now. `drain`: the start time of the drain that
    moves it, kept in the edit so its edits are told from another drain's of the same project."""
    now = time.time() if now is None else now
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        c = next((c for c in doc.get("changes", []) if isinstance(c, dict) and c.get("id") == cid
                  and c.get("kind") == POWER_CYCLE and not c.get("closed")), None)
        if c is None:
            raise ValueError(f"no open power-cycle intent #{cid}")
        what = str(c.get("what") or "")
        new = {"at": at, "expires": at + POWER_CYCLE_AFTER_S,
               "what": f"power-cycle intent, at {ends.shown(at)}" + (f": {what.split(': ', 1)[1]}" if ": " in what else "")}
        if hold_from is not None:
            new["hold_from"] = min(cycle_window(c)[0], float(hold_from))
        c.setdefault("edits", []).append({"at": now, "by": by, "was": {k: c.get(k) for k in new}}
                                         | ({"drain": drain} if drain is not None else {}))
        c.update(new)
        _write(LEDGER, doc)
        return c


# The fields a retime moves (retime_power_cycle) and a restore puts back (restore_power_cycle).
RETIMED = ("at", "expires", "what", "hold_from")


def restore_power_cycle(cid: int, was: dict, by: str, mark: str, now: float | None = None,
                        edits: int | None = None) -> dict:
    """Put open power-cycle intent #cid's time fields (RETIMED) back to `was` (a None or missing field is
    removed), with an edit trail entry that carries `mark` as `undo`; once per mark: a second call with the
    same mark changes nothing. `edits`: the length of the edit trail `was` was worked out from; when the
    trail has grown since, nothing changes either (the caller works it out again)."""
    now = time.time() if now is None else now
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        c = next((c for c in doc.get("changes", []) if isinstance(c, dict) and c.get("id") == cid
                  and c.get("kind") == POWER_CYCLE and not c.get("closed")), None)
        if c is None:
            raise ValueError(f"no open power-cycle intent #{cid}")
        if any(e.get("undo") == mark for e in c.get("edits") or []) \
                or edits is not None and len(c.get("edits") or []) != edits:
            return c
        c.setdefault("edits", []).append({"at": now, "by": by, "was": {k: c.get(k) for k in RETIMED}, "undo": mark})
        for k in RETIMED:
            if was.get(k) is None:
                c.pop(k, None)
            else:
                c[k] = was[k]
        _write(LEDGER, doc)
        return c


# queued requests (see Queued requests) ------------------------------------------------------------

# fields a retry of the same command may change (it runs later): left out of its request id
UNHASHED = ("added", "at", "expires", "since")


def request_id(op: str, fields: dict) -> str:
    """The id of request `op` with `fields`: the same command run again gets the same one."""
    key = {k: v for k, v in fields.items() if k not in UNHASHED}
    return hashlib.sha256(json.dumps([op, key], sort_keys=True, default=str).encode()).hexdigest()[:16]


def _queue(state: Path, op: str, fields: dict) -> dict:
    """Queue `op` for the project daemon in `state` (once per request id); returns the request."""
    rid = request_id(op, fields)
    req = {"queued": rid, "op": op, "fields": fields}
    if op == "add":
        made = next((c for c in changes() if c.get("request") == rid), None)
        if made:
            return made   # recorded already: a retry adds nothing
    path = Path(state) / REQUESTS / f"{rid}.json"
    try:
        return {**json.loads(path.read_text()), "queued": rid}   # queued already
    except (OSError, ValueError):
        pass
    try:
        project.write_json(path, req, 0o600)
    except OSError as e:
        raise ValueError(f"the shared machine ledger is read-only here ({_path(LEDGER).parent}) and the request "
                         f"could not be queued in {path.parent} ({e.strerror}): run the command where the "
                         f"ledger is writable, or ask the coordinator to") from None
    return req


def pending(state: Path) -> list[dict]:
    """The requests queued in project state dir `state`, oldest first."""
    def age(f: Path) -> float:
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0
    out = []
    for f in sorted(Path(state, REQUESTS).glob("*.json"), key=age):
        if f.name.endswith(".failed.json"):
            continue
        try:
            req = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(req, dict) and isinstance(req.get("fields"), dict):
            out.append({**req, "queued": f.stem})
    return out


def apply_requests(state: Path, log: Callable[[str], None] = print) -> list[dict]:
    """Record the requests queued in project state dir `state` (oldest first) in the ledger, each once, and
    remove it; one the ledger refuses is kept as `<id>.failed.json` with the reason. Returns the entries."""
    done = []
    for req in pending(state):
        rid, f, path = req["queued"], req["fields"], Path(state, REQUESTS, req["queued"] + ".json")
        try:
            if req.get("op") == "add":
                if f.get("alias") not in machines.load():
                    raise ValueError(f"no machine {f.get('alias')!r} in `ttp machines list`")
                c = _store_change(f, request=rid)
                log(f"machine change #{c['id']} on {c['alias']} recorded from queued request {rid}")
            elif req.get("op") == "test":
                c = _set_test(int(f["id"]), f.get("by") or "", bool(f.get("test")), float(f.get("at") or time.time()))
                log(f"machine change #{c['id']} test flag set to {bool(f.get('test'))} from queued request {rid}")
            elif req.get("op") == "close":
                try:
                    c = _close(int(f["id"]), f.get("why") or "", f.get("by") or "", float(f.get("at") or time.time()))
                    log(f"machine change #{c['id']} closed from queued request {rid}")
                except ValueError:
                    c = {"id": f.get("id")}   # closed already: by an earlier run of this request, or by someone else
            else:
                raise ValueError(f"unknown request {req.get('op')!r}")
        except (ValueError, TypeError, KeyError) as e:
            log(f"queued machine-ledger request {rid} refused: {e}")
            project.write_json(path.with_name(f"{rid}.failed.json"), {**req, "error": str(e)}, 0o600)
        else:
            done.append(c)
        path.unlink(missing_ok=True)
    return done


END_HINT = "give an end: --expires <delay or time>, --until-probe <command> or --until-task <id>"


def check_task(p, tid: int) -> None:
    """Refuse an until-task that is not an open task of project `p` (the entry's author)."""
    row = p.db.task(int(tid))
    if row is None:
        raise ValueError(f"no task #{tid} in project {p.name}: --until-task names one of its open tasks")
    if row["status"] in TERMINAL_TASK_STATES:
        raise ValueError(f"task #{tid} is {row['status']} already: give an open task, or another end")


def task_over(p, tid: Any) -> str:
    """Why until-task `tid` of project `p` is over ("" while it is open): done, failed, cancelled or gone."""
    try:
        row = p.db.task(int(tid))
    except (TypeError, ValueError):
        return f"its until-task #{tid} is not a task id"
    if row is None:
        return f"its until-task #{tid} is gone"
    return f"its until-task #{tid} is {row['status']}" if row["status"] in TERMINAL_TASK_STATES else ""


def changes(aliases=None, open_only: bool = True) -> list[dict]:
    doc = _read(LEDGER, {"changes": []})
    return [c for c in doc.get("changes", []) if isinstance(c, dict) and (not open_only or not c.get("closed"))
            and (aliases is None or c.get("alias") in aliases)]


def close_change(cid: int, why: str = "", by: str = "", now: float | None = None,
                 queue: Path | None = None) -> dict:
    """Close open entry #cid. `queue`: as add_change's (the request is returned, with `queued`)."""
    now = time.time() if now is None else now
    try:
        return _close(cid, why, by, now)
    except OSError as e:
        if queue is None or e.errno not in UNWRITABLE:
            raise
        if not any(c.get("id") == cid for c in changes()):
            raise ValueError(f"no open machine change #{cid}") from None
        return _queue(queue, "close", {"id": cid, "why": why, "by": by, "at": now})


def _close(cid: int, why: str, by: str, now: float) -> dict:
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        for c in doc.get("changes", []):
            if c.get("id") == cid and not c.get("closed"):
                c.update(closed=now, closed_why=_one_line(why) or "closed", closed_by=by)
                _write(LEDGER, doc)
                return c
    raise ValueError(f"no open machine change #{cid}")


def set_test(cid: int, by: str, test: bool, now: float | None = None, queue: Path | None = None) -> dict:
    """Flag entry #cid, open or closed, as made for a test (`test`) or not, after the fact: `ttp ttr` then labels
    the incidents inside its window afresh. Only its author or its machine's recovery owner (`by`) may. Keeps
    the entry; appends {at, by, was: {test}, test} to its `edits`. `queue`: as add_change's."""
    now = time.time() if now is None else now
    c = next((c for c in changes(open_only=False) if c.get("id") == cid), None)
    if c is None:
        raise ValueError(f"no machine change #{cid}")
    _test_owner_ok(c, by)
    if bool(c.get("test")) == bool(test):
        raise ValueError(f"machine change #{cid} is {'already' if test else 'not'} flagged as a test: nothing to change")
    try:
        return _set_test(cid, by, bool(test), now)
    except OSError as e:
        if queue is None or e.errno not in UNWRITABLE:
            raise
        return _queue(queue, "test", {"id": cid, "test": bool(test), "by": by, "at": now})


def _test_owner_ok(c: dict, by: str) -> None:
    owner = (machines.load().get(c.get("alias")) or {}).get("recovery_owner")
    if not by or by not in (c.get("by"), owner):
        raise ValueError(f"machine change #{c.get('id')} was recorded by {c.get('by') or '?'}: only that project"
                         + (f" or {c.get('alias')}'s recovery owner ({owner})" if owner else "")
                         + f" may flip its test flag, not {by or 'an unnamed caller'}; run it inside that project")


def _set_test(cid: int, by: str, test: bool, now: float) -> dict:
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        c = next((c for c in doc.get("changes", []) if isinstance(c, dict) and c.get("id") == cid), None)
        if c is None:
            raise ValueError(f"no machine change #{cid}")
        _test_owner_ok(c, by)
        was = bool(c.get("test"))
        if was == test:
            return c   # flipped already (a queued request run again, or by someone else)
        if test:
            c["test"] = True
        else:
            c.pop("test", None)
        c.setdefault("edits", []).append({"at": now, "by": by, "was": {"test": was}, "test": test})
        _write(LEDGER, doc)
        return c


def edit_change(cid: int, by: str, what: str | None = None, undo: str | None = None, expires: Any = None,
                until_probe: str | None = None, now: float | None = None, until_task: int | None = None,
                where=None, auto_undo: bool | None = None, project=None, allow_met_probe: bool = False,
                warn: Callable[[str], None] | None = None) -> dict:
    """Correct open entry #cid in place: its text, undo command or end (--expires, --until-probe or
    --until-task, as add_change takes them, an --until-probe with an --expires too; the same until-task again
    restarts its PROBE_END_S, so a continuation task re-extends the entry, and the same until-probe again keeps
    its end but is judged afresh). Keeps its id, alias, author and creation time; records who edited it, when
    and the values it replaced in `edits`. A new end starts afresh (`page_from`): its until-probe result and overdue
    pages are dropped. Refuses a closed or unknown id, a power-cycle intent's undo or end, and an edit
    that changes nothing. `where`: the zone a zone-less --expires is read in (ends.parse_expires). `auto_undo`:
    turn the auto-undo on (True, checked by check_auto_undo against the entry's undo and probe as edited) or
    off (False); refused while one runs. A new undo, end or auto-undo setting starts its tries afresh. `project`
    (a Project): run a given until-probe once first, outside the ledger lock, as add_change does
    (check_probe_unmet; `allow_met_probe`, `warn`); refused, the entry is left unchanged."""
    now = time.time() if now is None else now
    if sum(map(bool, (expires, until_probe, until_task))) > 1 and not (expires and until_probe and not until_task):
        raise ValueError("give one end: --expires, --until-probe (with or without --expires) or --until-task")
    new: dict[str, Any] = {}
    if what is not None:
        new["what"] = _one_line(what)
    if undo is not None:
        new["undo"] = _one_line(undo, 1000)
    if not all(new.values()):
        raise ValueError("the text and the --undo command cannot be empty")
    if until_probe is not None:
        probe = _one_line(until_probe, ends.PROBE_CHARS)
        if not probe:
            raise ValueError("the --until-probe command cannot be empty")
        ends.check_sh(probe, "--until-probe")
        ends.check_not_trivial(probe, "--until-probe")
        new.update(probe=probe, until_task=None, expires=now + PROBE_END_S if not expires else
                   float(expires) if isinstance(expires, (int, float)) else ends.parse_expires(expires, now, where=where))
    elif until_task:
        new.update(probe=None, until_task=int(until_task), expires=now + PROBE_END_S)
    elif expires:
        new.update(probe=None, until_task=None, expires=float(expires) if isinstance(expires, (int, float))
                   else ends.parse_expires(expires, now, where=where))
    if auto_undo is not None:
        new["auto_undo"] = bool(auto_undo)
    if not new:
        raise ValueError("say what to change: --what, --undo, --expires, --until-probe, --until-task or --auto-undo")
    if "probe" in new and project is not None:   # not for an id the locked read below refuses anyway
        cur = next((c for c in _read(LEDGER, {}).get("changes", []) if isinstance(c, dict) and c.get("id") == cid), None)
        if cur and not cur.get("closed") and cur.get("kind") != POWER_CYCLE:
            check_probe_unmet(project, new["probe"], allow_met_probe, warn)
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        c = next((c for c in doc.get("changes", []) if isinstance(c, dict) and c.get("id") == cid), None)
        if c is None:
            raise ValueError(f"no machine change #{cid}")
        if c.get("closed"):
            raise ValueError(f"machine change #{cid} is closed; record a new one with `ttp machines change add`")
        if c.get("kind") == POWER_CYCLE and set(new) - {"what"}:
            raise ValueError(f"machine change #{cid} is a planned power cycle: only --what can change; "
                             f"close it and record a new one for another time")
        if c.get("kind") == POWER_CYCLE and auto_undo:
            raise ValueError(f"machine change #{cid} is a planned power cycle: it has no undo to run")
        if c.get("auto_undo_claim") and {"undo", "auto_undo"} & set(new):
            raise ValueError(f"machine change #{cid}: its auto-undo is running (claimed "
                             f"{ends.shown(float(c['auto_undo_claim'].get('at') or now), where)}); edit it once it ended")
        if new.get("auto_undo", c.get("auto_undo")) and ({"undo", "auto_undo", "probe", "until_task"} & set(new)):
            check_auto_undo(new.get("undo", c.get("undo")), new.get("probe", c.get("probe")),
                            new.get("until_task", c.get("until_task")))
        reprobe = until_probe is not None and new["probe"] == c.get("probe")
        if reprobe:   # the same until-probe again keeps its end (unless --expires gives one) but starts afresh
            del new["probe"]
            if not expires:
                del new["expires"]
        was = {k: c.get(k) for k, v in new.items() if (bool(c.get(k)) if k == "auto_undo" else c.get(k)) != v}
        if reprobe:
            was["probe_passed"] = c.get("probe_passed")
        if not was:
            raise ValueError(f"machine change #{cid} already says that: nothing to change")
        for k in set(was) & set(new):
            c[k] = new[k]
        if c.get("auto_undo") is False:
            del c["auto_undo"]
        if "expires" in was or reprobe or {"undo", "auto_undo"} & set(was):
            for k in AUTO_UNDO_STATE:   # a new undo or end gets its tries afresh (its runs stay in the history)
                c.pop(k, None)
        if "expires" in was or reprobe:
            for k in ("probe_passed", "probe_job_failed", "probe_out", "task_ended", "task_status", "page"):
                c.pop(k, None)
            c["page_from"] = now   # a new page episode: its pages get ids of their own, not the delivered ones'
        c.setdefault("edits", []).append({"at": now, "by": by, "was": was})
        _write(LEDGER, doc)
        return c


def overdue(c: dict, now: float | None = None, where=None) -> str:
    """Why entry `c` is overdue ("" when it is not)."""
    now = time.time() if now is None else now
    if c.get("closed") or c.get("kind") == POWER_CYCLE:   # a power cycle has nothing to undo: it closes itself
        return ""
    if trivial_probe(c):   # a pass of a probe that always exits 0 says nothing: its end is only its time
        pass
    elif c.get("probe_job_failed"):
        return f"the detached job its until-probe waits on ended but failed ({c['probe_job_failed'] if isinstance(c['probe_job_failed'], str) else 'probe exit ' + str(c['probe_job_failed'])})"
    elif c.get("probe_passed"):
        return "its until-probe passed"
    if c.get("task_ended"):
        return str(c.get("task_end_why") or f"its until-task #{c.get('until_task')} ended")
    if float(c.get("expires") or 0) <= now:
        return f"expired {ends.shown(float(c['expires']), where)}"
    return ""


def expects_now(c: dict, now: float | None = None) -> list[str]:
    """What entry `c` expects at `now`: nothing once it is closed or overdue, and for a power-cycle intent
    nothing outside POWER_CYCLE_LEAD_S before its time until its end, so it never quiets an outage
    days before the cycle, or after it when nobody closed it. A drain's `hold_from` does not widen this:
    the drain moves the time to the cycle once it records it (hostcycle.cycle_now)."""
    now = time.time() if now is None else now
    if c.get("closed") or overdue(c, now):
        return []
    if c.get("kind") == POWER_CYCLE:
        at = float(c.get("at") or 0)
        if not at - POWER_CYCLE_LEAD_S <= now < float(c.get("expires") or at + POWER_CYCLE_AFTER_S):
            return []
    return [str(x) for x in c.get("expects") or []]


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:max(n - 1, 0)].rstrip(" ,;:") + "…"


def trivial_probe(c: dict) -> bool:
    """Whether open entry `c` waits on an until-probe that always exits 0 (ends.always_succeeds). One stored
    before add and edit refused them never ends the entry: Probes reports it instead of running it."""
    return bool(c.get("probe")) and not c.get("closed") and ends.always_succeeds(c["probe"])


# An undo that does nothing: empty, `true`, `:`, or `none` (any case) alone or followed by `:`, `;`, `-` or
# whitespace and a reason (`none: nothing left to undo`). Nothing else, not `true && x` nor `none_cleanup.sh`.
NOOP_UNDO = re.compile(r"(?:true|:|(?i:none)(?:[:;\s-].*)?)?", re.S)
AUTO_CLOSE = "auto-close"   # closed_by of an entry route closed itself: f"{AUTO_CLOSE}: <rule>"


def noop_undo(undo: Any) -> bool:
    """Whether undo command `undo` does nothing (NOOP_UNDO, after trimming whitespace)."""
    return isinstance(undo, str) and NOOP_UNDO.fullmatch(undo.strip()) is not None


def _due(c: dict, now: float) -> str:
    """Why open entry `c`'s undo is due with nobody paged ("" when it is not): its until-probe passed
    PROBE_PASSES times in a row (never a probe that always exits 0, nor a pass whose job failed), or it has no
    until-probe or until-task and its time passed. A still failing, broken or timed out probe never makes it
    due, even past its --expires."""
    if c.get("closed") or c.get("kind") == POWER_CYCLE:
        return ""
    if c.get("probe"):
        ok = c.get("probe_passed") and not c.get("probe_job_failed") and not trivial_probe(c)
        return f"until-probe passed {PROBE_PASSES} times" if ok else ""
    if c.get("until_task"):
        return ""
    return "expired" if float(c.get("expires") or 0) <= now else ""


def auto_close_rule(c: dict, now: float) -> str:
    """The rule that closes open entry `c` with no page ("" when none does): its undo is a no-op and it is
    due (_due), or its project's daemon saw its until-task done, failed or cancelled (task_status; never a
    task it could not resolve, which pages as before)."""
    if not noop_undo(c.get("undo")):
        return ""
    due = _due(c, now)
    if not due and c.get("until_task") and c.get("task_ended") and c.get("task_status") in TERMINAL_TASK_STATES \
            and not c.get("closed"):
        due = f"its until-task #{c['until_task']} is {c['task_status']}"
    return f"no-op undo, {due}" if due else ""


# Auto-undo (module doc): the recording project's daemon runs an --auto-undo entry's undo once it is due.
AUTO_UNDO = "auto-undo"         # closed_by of an entry its auto-undo closed; closed_why "auto-undo ok"
AUTO_UNDO_TIMEOUT_S = 300       # an undo still running after this is killed, its whole process group
AUTO_UNDO_TRIES = 3             # runs that exit 1, 75 or 255 (not yet, in use, host unreachable) before a page
AUTO_UNDO_RETRY_RCS = (1, 75, 255)
AUTO_UNDO_TAIL_CHARS = 2000     # of each run's output, kept in its history
AUTO_UNDO_PAGE_TAIL_CHARS = 600   # of the last run's output, quoted in the page
AUTO_UNDO_RUNS_KEPT = 10        # history entries kept per change
# the per-episode state an edit of the undo or the end clears (the run history stays)
AUTO_UNDO_STATE = ("auto_undo_claim", "auto_undo_tries", "auto_undo_next", "auto_undo_failed")


def check_auto_undo(undo: Any, probe: Any = None, until_task: Any = None) -> None:
    """Refuse --auto-undo for an undo that does nothing (the auto-close covers it), one that kills by name or
    pattern (killscan: unattended, it could end other projects' work), an until-probe that always exits 0
    (it would run the undo at once, while the change is still in use), or an until-task end (never due
    unpaged: _due)."""
    if noop_undo(undo):
        raise ValueError("--auto-undo needs an undo that does something: a no-op undo (empty, `true`, `:` or "
                         "`none: <why>`) already closes itself once its end passes")
    from . import killscan
    hits = killscan.scan(str(undo))
    if hits:
        raise ValueError(f"--auto-undo refuses an undo that kills by name or pattern ({hits[0][1]}: "
                         f"`{hits[0][2][:120]}`): run unattended, it could end other work; kill by a recorded pid, "
                         f"or leave out --auto-undo and undo it yourself")
    if probe and ends.always_succeeds(str(probe)):
        raise ValueError("--auto-undo needs an until-probe that can say \"not yet\": this one always exits 0, so "
                         "the undo would run at once; end the test with `|| exit 1`")
    if until_task:
        raise ValueError("--auto-undo runs once an --until-probe passed or an --expires passed; an --until-task "
                         "end is paged instead: give the entry one of those ends, or leave out --auto-undo")


def auto_undo_due(c: dict, now: float) -> str:
    """Why open entry `c`'s auto-undo should run now ("" when not): it has --auto-undo, a real undo, is due
    (_due), has not given up (auto_undo_failed) and no retry waits (auto_undo_next)."""
    if not c.get("auto_undo") or c.get("auto_undo_failed") or noop_undo(c.get("undo")):
        return ""
    if float(c.get("auto_undo_next") or 0) > now:
        return ""
    return _due(c, now)


def auto_undo_waits(c: dict, now: float) -> bool:
    """Whether route leaves overdue entry `c` to its project's auto-undo (no page): it runs, or it is still due
    (a retry waits only while it is), and has not given up. One no longer due (its until-probe says "not yet"
    again) is paged as any overdue entry."""
    if not c.get("auto_undo") or c.get("auto_undo_failed") or c.get("closed") or noop_undo(c.get("undo")):
        return False
    return bool(c.get("auto_undo_claim") or _due(c, now))


def auto_undo_paused(c: dict, p, now: float) -> str:
    """Why `c`'s auto-undo must wait ("" when it may run): a known outage of its machine, a machine-wide hold,
    or a pause of a resource on it (this project's own, or a shared one)."""
    from . import fleethold, shared
    from . import responsibilities as rs
    alias = c.get("alias") or ""
    o = outages(now).get(alias)
    if o:
        return f"known outage of {alias}: {o.get('why')}"
    if fleethold.active().get(alias):
        return f"{alias} is held machine-wide"
    on = on_machine(alias)
    db = getattr(p, "db", None)
    for res in sorted((db.paused_resources() if db is not None else {}) or {}):
        if on(res):
            return f"resource {res} is paused"
    for res in sorted(rs._shared_resources(alias)):
        if shared.read_pause(res):
            return f"shared resource {res} is paused"
    return ""


def change_line(c: dict, now: float | None = None, where=None, clip: int | None = None) -> str:
    """Entry `c` on one line. `clip`: at most about that many characters (the coordinator digest): the
    reason is cut first, then a long undo, probe or overdue cause, so the id, alias, author, the start of
    the reason, the undo and the end always show."""
    late = overdue(c, now, where)
    part = (lambda t: _clip(t, CLIP_PART_CHARS)) if clip else str
    end = f"until-probe `{part(c['probe'])}`, at the latest {ends.shown(c['expires'], where)}" if c.get("probe") \
        else f"until task #{c['until_task']} ends, at the latest {ends.shown(c['expires'], where)}" \
        if c.get("until_task") else f"ends {ends.shown(c['expires'], where)}"
    by = c.get("by", "?") + (f" #{c['task']}" if c.get("task") else "")
    end += f"; expects {', '.join(c['expects'])}" if c.get("expects") else ""
    end += "; a test" if c.get("test") else ""
    end += ("; auto-undo" + (f" gave up: {part(c['auto_undo_failed'].get('why'))}" if c.get("auto_undo_failed")
                             else "")) if c.get("auto_undo") else ""
    end += (f"; its until-probe always exits 0, so it never ends this: fix it with `ttp machines change edit "
            f"{c['id']} --until-probe '<test> || exit 1'`") if trivial_probe(c) else ""
    end += f"; backdated to {ends.shown(c['since'], where)}" if isinstance(c.get("since"), (int, float)) else ""
    head, tail = f"#{c['id']} {c['alias']}: ", (f" (by {by}; undo: `{part(c['undo'])}`; {end})"
                                                + (f" OVERDUE: {part(late)}" if late else ""))
    what = _clip(c["what"], max(clip - len(head) - len(tail), CLIP_WHAT_MIN)) if clip else c["what"]
    return head + what + tail


# conditions ---------------------------------------------------------------------------------------

def report(by: str, alias: str, condition: str, text: str = "", grace_s: float | None = None,
           cleared: bool = False, now: float | None = None, known: str | None = None,
           escalate: str | None = None) -> bool:
    """A co-tenant `by` sees `alias` `condition` (down or held), or no longer does (`cleared`). `known`: why
    the reporter knows the fault (its heal check's known_fault); "" says it does not, None keeps it.
    `escalate`: who the reporting heal check escalates to ("coordinator" takes the user's page).
    Returns whether the alias is a known machine."""
    now = time.time() if now is None else now
    if condition not in CONDITIONS or alias not in machines.load():
        return False
    key = f"{alias}:{condition}"
    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        conds = doc.setdefault("conditions", {})
        if cleared:
            if conds.pop(key, None) is None:
                return True
        else:
            c = conds.setdefault(key, {"alias": alias, "condition": condition, "first": now, "seen_by": []})
            c.update(last=now, text=_one_line(text) or c.get("text", ""),
                     grace_s=float(grace_s if grace_s is not None else c.get("grace_s", GRACE_S)))
            if by not in c["seen_by"]:
                c["seen_by"].append(by)
            if known is not None:
                kb = c.setdefault("known_by", {})
                if _one_line(known):
                    kb[by] = _one_line(known)
                else:
                    kb.pop(by, None)
            if escalate is not None:
                eb = c.setdefault("escalate_by", {})
                if escalate == "coordinator":
                    eb[by] = escalate
                else:
                    eb.pop(by, None)
        _write(ROUTES, doc)
    return True


def conditions() -> dict[str, dict]:
    return _read(ROUTES, {"conditions": {}}).get("conditions", {})


# known outages ------------------------------------------------------------------------------------

def set_outage(alias: str, why: str, by: str, expires: Any, owner: str = "", now: float | None = None,
               covers: Any = None, where=None) -> dict:
    """Record that `alias` is out for a known reason (a hardware repair) until `expires`: nobody is paged
    about it meanwhile. `by`: the project recording it; each project keeps its own entry per machine (module
    doc: Known outages), so this replaces only `by`'s. `owner`: who has the repair, often a human or an
    external ticket. `covers`: the parts it explains (OUTAGE_COVERS, or a service by name), stored for
    co-tenant watchers and heal checks; none: the whole machine. `where`: the zone a zone-less `expires` is
    read in (ends.parse_expires). Returns the entry."""
    now = time.time() if now is None else now
    if alias not in machines.load():
        raise ValueError(f"no machine {alias!r} in `ttp machines list`")
    why = _one_line(why)
    if not why:
        raise ValueError("say why the machine is out")
    if not expires:
        raise ValueError("give an end: --expires <delay or time>")
    parts = outage_covers(covers)
    by = _one_line(by, 120) or "user"
    rec = {"why": why, "owner": _one_line(owner, 120), "by": by, "since": now,
           "until": ends.parse_expires(expires, now, where=where)}
    if parts:
        rec["covers"] = parts
    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        got = outage_entries(now, doc)
        got.setdefault(alias, {})[by] = rec
        _store_outages(doc, got)
        _write(ROUTES, doc)
    return rec


def outage_covers(covers: Any) -> list[str]:
    """`covers` (a list, or comma-separated words): the OUTAGE_COVERS in that order, then service names."""
    if isinstance(covers, str):
        covers = [covers]
    words = {w.strip().lower() for c in (covers or []) for w in str(c).split(",") if w.strip()}
    bad = sorted(w for w in words - set(OUTAGE_COVERS) if not COVER_NAME.fullmatch(w))
    if bad:
        raise ValueError(f"--covers takes {', '.join(OUTAGE_COVERS)} or a service's name (one word); "
                         f"not {', '.join(bad)}")
    return [c for c in OUTAGE_COVERS if c in words] + sorted(words - set(OUTAGE_COVERS))


def clear_outage(alias: str, by: str | None = None, now: float | None = None) -> bool:
    """End `by`'s outage entry on `alias` (None: every project's). Whether one was open."""
    now = time.time() if now is None else now
    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        got = outage_entries(now, doc)
        mine = got.get(alias) or {}
        if not mine or (by is not None and by not in mine):
            return False
        if by is None:
            got.pop(alias)
        else:
            mine.pop(by)
            if not mine:
                got.pop(alias)
        _store_outages(doc, got)
        _write(ROUTES, doc)
    return True


def outage_entries(now: float | None = None, doc: dict | None = None) -> dict[str, dict[str, dict]]:
    """{alias: {project: entry}} of the known outages not yet past their end, each project's on its own.
    The routes file keeps them in OUTAGE_ENTRIES and, for co-tenants on an older runtime, one merged record
    per machine in "outages" (merged_outage, marked by its "entries"). A record there without that mark was
    written by an older runtime (or before entries existed): it is that project's entry, so a single old
    record migrates without loss. A machine missing from "outages" was cleared by an older runtime."""
    now = time.time() if now is None else now
    doc = doc if doc is not None else _read(ROUTES, {"conditions": {}})
    legacy = doc.get("outages") or {}
    stored = doc.get(OUTAGE_ENTRIES) or {}
    out: dict[str, dict[str, dict]] = {}
    for alias, o in legacy.items():
        if not isinstance(o, dict):
            continue
        mine = {by: dict(e) for by, e in (stored.get(alias) or {}).items() if isinstance(e, dict)}
        if "entries" not in o and o.get("why"):
            mine[str(o.get("by") or "user")] = {k: v for k, v in o.items() if k != "entries"}
        mine = {by: e for by, e in mine.items() if float(e.get("until") or 0) > now}
        if mine:
            out[alias] = mine
    return out


def merged_outage(entries: dict[str, dict]) -> dict:
    """One machine's open entries as one record, for readers of a single outage: out until the last ends,
    since the first began; the reason and recording projects of each; the first owner given; covers only
    what every entry explains together (an entry without covers: the whole machine). "entries": each entry
    with its project, earliest first."""
    es = sorted(entries.values(), key=lambda e: (float(e.get("since") or 0), str(e.get("by") or "")))
    if len(es) == 1:
        rec = dict(es[0])
    else:
        rec = {"why": "; ".join(f"{e.get('by') or '?'}: {e.get('why') or ''}" for e in es),
               "owner": next((e["owner"] for e in es if e.get("owner")), ""),
               "by": ", ".join(str(e.get("by") or "?") for e in es),
               "since": min(float(e.get("since") or 0) for e in es),
               "until": max(float(e.get("until") or 0) for e in es)}
        if all(e.get("covers") for e in es):
            rec["covers"] = outage_covers([c for e in es for c in e["covers"]])
    rec["entries"] = [dict(e) for e in es]
    return rec


def _store_outages(doc: dict, got: dict[str, dict[str, dict]]) -> None:
    doc[OUTAGE_ENTRIES] = got
    doc["outages"] = {a: merged_outage(es) for a, es in got.items()}


def outages(now: float | None = None, doc: dict | None = None) -> dict[str, dict]:
    """{alias: merged record (merged_outage)} of the machines with a known outage not yet past its end: out
    while any project's entry is open."""
    return {a: merged_outage(es) for a, es in outage_entries(now, doc).items()}


def outage_line(alias: str, o: dict, where=None) -> str:
    """One machine's known outage on one line; with entries from several projects, each with its project,
    owner, covers and end."""
    def one(e: dict) -> str:
        owner = f"; owner: {e['owner']}" if e.get("owner") else ""
        owner += f"; covers: {', '.join(e['covers'])}" if e.get("covers") else ""
        return f"until {ends.shown(e['until'], where)} (by {e.get('by') or '?'}{owner}): {e['why']}"
    es = o.get("entries") or []
    if len(es) > 1:
        return f"{alias}: known outages from {len(es)} projects: " + "; ".join(one(e) for e in es)
    return f"{alias}: known outage {one(es[0] if es else o)}"


def on_machine(alias: str) -> Callable[[str], bool]:
    """Whether a resource name is on machine `alias` (names it, or its hostname)."""
    from . import responsibilities as rs
    rec = machines.load().get(alias) or {}
    needles = [alias] + ([str(rec["hostname"])] if rec.get("hostname") else [])
    return lambda res: any(rs._names(n, res) for n in needles)


def _demand(alias: str, reporters: list[str]) -> dict[str, list[str]]:
    """Read-only, from every project on this machine: the open tasks that use a resource on `alias`
    ("need"), and the pauses of resources on it ("paused", "user": set by the user). A reporter that
    cannot be read from here counts as needing it."""
    from . import globalcap, shared
    from . import responsibilities as rs
    from .coordinator import task_resources
    from .db import PAUSED_RESOURCES_KEY
    on = on_machine(alias)
    out: dict[str, list[str]] = {"need": [], "paused": [], "user": [], "shared": []}
    seen = set()
    for name, base in rs._projects(project.hostname()):
        seen.add(name)
        if not (base / "state" / "project.db").is_file():
            continue
        try:
            db = rs.ReadOnly(globalcap.connect_ro(base / "state" / "project.db"))
        except Exception:
            out["need"].append(f"{name} (its state cannot be read)")
            continue
        try:
            pz = db.kv(PAUSED_RESOURCES_KEY, {}) or {}
            pz = pz if isinstance(pz, dict) else {}
            for res, v in sorted(pz.items()):
                if on(res):
                    out["user" if isinstance(v, dict) and v.get("by") == "user" else "paused"].append(f"{name} {res}")
            for t in db.q("SELECT id, labels FROM tasks WHERE status NOT IN ('done','failed','cancelled')"):
                for res in sorted(task_resources(t)):
                    if on(res) and res not in pz and not shared.read_pause(res):
                        out["need"].append(f"{name} #{t['id']} ({res})")
        except Exception as e:
            out["need"].append(f"{name} (its state cannot be read: {type(e).__name__})")
        finally:
            db.close()
    for alias_res in sorted({r for r in rs._shared_resources(alias)}):
        sp = shared.read_pause(alias_res)
        if sp:
            out["user" if sp.get("by") == "user" else "shared"].append(f"shared {alias_res}")
    out["need"] += [f"{who} (not readable from here)" for who in reporters if who not in seen]
    return out


def quiet_reason(c: dict, now: float, doc: dict | None = None) -> tuple[str, bool]:
    """(why condition `c` escalates no further, whether nobody is paged at all); ("", False) when it pages as
    usual (module doc: Quiet outages)."""
    alias, cond = c["alias"], c["condition"]
    o = outages(now, doc).get(alias)
    if o:
        return (f"known outage until {ends.shown(o['until'])}"
                + (f", owned by {o['owner']}" if o.get("owner") else "") + f": {o['why']}"), True
    for ch in changes({alias}):
        if cond in expects_now(ch, now):
            return f"expected from machine change #{ch['id']} by {ch.get('by')}: {ch['what']}", True
    from . import fleethold
    hold = fleethold.active().get(alias)
    if hold:   # like a user pause: the first target hears of it, nobody past it
        return (f"held machine-wide (hold {hold.get('id')}) by {hold.get('project')}"
                + (" for the user" if hold.get("by") == "user" else "")
                + (f": {hold['reason']}" if hold.get("reason") else "")), False
    d = _demand(alias, list(c.get("seen_by") or []))
    if d["user"]:
        return f"the user paused {', '.join(d['user'])}", False
    if d["shared"]:
        return f"paused for every project: {', '.join(d['shared'])}", False
    known = c.get("known_by") or {}
    if known:
        return "known fault: " + "; ".join(f"{who}: {why}" for who, why in sorted(known.items())), False
    if not d["need"]:
        return "no open work uses it" + (f" (paused: {', '.join(d['paused'])})" if d["paused"] else ""), False
    return "", False


# who can act ---------------------------------------------------------------------------------------

def can_act(name: str, here: str | None = None) -> tuple[bool, str]:
    """Whether project `name` can take a page now, read-only from its own state: its daemon runs and
    ticks, it is not paused, and its coordinator is not logged out or failing. A project on another
    machine cannot be checked from here and counts as able; a name not in the registry cannot act."""
    from . import globalcap, overview
    here = here or project.hostname()
    entry = project.load_registry().get("projects", {}).get(name)
    if not isinstance(entry, dict):
        return False, "not a project of this user"
    if entry.get("host") and entry["host"] != here:
        return True, ""
    state = Path(entry.get("dir") or "") / project.FOLDER / "state"
    if not entry.get("dir") or not (state / "project.db").is_file():
        return False, "its state cannot be read"
    try:
        conn = globalcap.connect_ro(state / "project.db")
    except Exception:
        return False, "its state cannot be read"
    try:
        status, _ = overview._daemon(conn, state, here)
        if status != "running":
            return False, f"its daemon is {status}"
        paused = conn.execute("SELECT value FROM kv WHERE key='paused'").fetchone()
        if paused and paused[0] not in (None, "", "null", "false", "0"):
            return False, "it is paused"
        bad = conn.execute("SELECT key FROM alerts WHERE cleared IS NULL AND (key LIKE 'auth:%' OR key='coordinator') "
                           "LIMIT 1").fetchone()
        if bad:
            return False, "its coordinator is logged out" if bad[0].startswith("auth:") else "its coordinator is failing"
    except Exception as e:
        return False, f"its state cannot be read ({type(e).__name__})"
    finally:
        conn.close()
    return True, ""


def chain(alias: str, first: str | None = None) -> list[str]:
    m = machines.load().get(alias) or {}
    out = []
    for name in (first, m.get("recovery_owner"), m.get("recovery_fallback")):
        if name and name not in out:
            out.append(name)
    return out


def _ask(able: Callable[[str], tuple[bool, str]], who: str) -> tuple[bool, str]:
    """able(who); any answer that is not an (ok, why) pair counts as cannot act, as can_act answers for a
    name it does not know."""
    got = able(who)
    if isinstance(got, (tuple, list)) and len(got) == 2:
        return bool(got[0]), str(got[1] or "")
    return False, "not a project of this user"


def _step(page: dict, targets: list[str], now: float, able: Callable[[str], tuple[bool, str]]) -> str | None:
    """Advance one item's page state; returns the target to page now, or None."""
    if "stage" not in page:
        page.update(stage=0, at=now)
        return targets[0] if targets else USER
    stage = int(page["stage"])
    if stage >= len(targets):
        return None   # the user has it
    ok, why = _ask(able, targets[stage])
    if ok:
        page.pop("unable_since", None)
        page.pop("why", None)
    else:
        page.setdefault("unable_since", now)
        page["why"] = why
    if (not ok and now - page["unable_since"] >= UNAVAILABLE_S) or now - page["at"] >= REPAGE_S:
        page.update(stage=stage + 1, at=now)
        page.pop("unable_since", None)
        return targets[stage + 1] if stage + 1 < len(targets) else USER
    return None


def _pending(page: dict, target: str, kind: str, key: str, alias: str, episode: float, text: str) -> None:
    """Record the page due for this step; it replaces one an earlier step left undelivered."""
    page["pending"] = {"target": target, "kind": kind, "key": key, "alias": alias, "stage": int(page["stage"]),
                       "id": f"{key}@{int(episode)}#{int(page['stage'])}", "text": text}


def _vouched(c: dict, targets: list[str], now: float,
             healthy: Callable[[str, str, float, str], list[str]]) -> tuple[str, list[str]] | None:
    """(project, its checks): the first project in the page chain, not itself reporting condition `c`, whose
    own heal check of the machine that observes that condition ran healthy and fresh; None when none does."""
    for who in targets:
        if who in c.get("seen_by", []):
            continue
        checks = healthy(who, c["alias"], now, c["condition"])
        if checks:
            return who, checks
    return None


def _task_host(c: dict, targets: list[str], able: Callable[[str], tuple[bool, str]]) -> str:
    """The project an outage task goes to: the fallback, else the first detecting project, that can act;
    else the fallback (or the first detector, else the owner) anyway."""
    owner = targets[0]
    rest = [t for t in targets[1:] if t != owner]
    seen = [s for s in c.get("seen_by") or [] if s != owner and s not in rest]
    for who in rest + seen:
        if _ask(able, who)[0]:
            return who
    return (rest + seen + [owner])[0]


def _task_step(c: dict, key: str, targets: list[str], page: dict, now: float,
               able: Callable[[str], tuple[bool, str]], b: dict, acts: list[dict],
               status: Callable[[str, str], str | None]) -> None:
    """Advance condition `c`'s outage task (outagetask.py): queue it once its owner cannot act, or did not
    record a recovery action within b['owner_s']; then, past its due time while open, one high page to its
    project, and past b['escalate_s'] more, one normal-severity user page (straight away when no project
    can act; to the reporter's coordinator when its heal check escalates there). A creating page still
    pending is never replaced."""
    task, owner = c.get("task"), targets[0]
    if task is None:
        ok, why = _ask(able, owner)
        if ok:
            if now - float(c["first"]) < b["owner_s"] or any(
                    a.get("alias") == c["alias"] and float(a.get("added") or 0) >= float(c["first"]) for a in acts):
                return
            why = (f"recovery owner {owner} recorded no recovery action (`ttp machines change add`) within "
                   f"{b['owner_s'] / 60:g} min of the outage's start")
        else:
            why = f"recovery owner {owner} cannot act: {why}"
        host, due = _task_host(c, targets, able), now + b["due_s"]
        paged = targets[:int(page.get("stage") or 0) + 1] if "stage" in page else []
        tried = (f"recovery page(s) to {', '.join(paged)}" if paged else "no recovery page yet") + \
            (f"; {page['why']}" if page.get("why") else "")
        rec = outagetask.record(c, key, why, tried, due)
        c["task"] = {"project": host, "at": now, "due": due, "label": rec["label"], "stage": 0,
                     "pending": {"target": host, "kind": outagetask.KIND, "key": f"machine:{key}",
                                 "alias": c["alias"], "stage": 0, "id": rec["label"], "outage": rec,
                                 "text": f"Machine {c['alias']} {c['condition']} since {ends.shown(c['first'])}: "
                                         f"{why}. A tracked outage task ({rec['label']}) is queued in {host}, "
                                         f"due {ends.shown(due)}."}}
        return
    stage, due = int(task.get("stage") or 0), float(task["due"])
    pend = task.get("pending")
    if stage >= 2 or now < due or (pend and pend.get("kind") == outagetask.KIND):
        return
    if outagetask.closed(status(task["project"], task["label"])):
        return
    late = f"Outage task {task['label']} in {task['project']} for machine {c['alias']} {c['condition']} " \
           f"(since {ends.shown(c['first'])}) is open past its due time {ends.shown(due)}"
    if stage == 0:
        target = next((w for w in [task["project"]] + list(c.get("seen_by") or []) if _ask(able, w)[0]), None)
        if target:
            task["stage"] = 1
            task["pending"] = {"target": target, "kind": outagetask.OVERDUE_KIND, "key": f"machine:{key}",
                               "alias": c["alias"], "stage": 1, "id": f"{task['label']}#overdue",
                               "severity": "high",
                               "text": f"{late}: check it and recover the machine now, or say who must act."}
            return
        tail = "; no project can act on it"   # nobody to wake: straight to the user, once
    elif now < due + b["escalate_s"]:
        return
    else:
        tail = f" by more than {b['escalate_s'] / 60:g} min"
    coordinators = [w for w in c.get("seen_by") or [] if (c.get("escalate_by") or {}).get(w) == "coordinator"]
    task["stage"] = 2
    task["pending"] = {"target": coordinators[0] if coordinators else USER, "kind": outagetask.OVERDUE_KIND, "key": f"machine:{key}",
                       "alias": c["alias"], "stage": 2, "id": f"{task['label']}#user", "severity": "normal",
                       "text": f"{late}{tail}."}

def route(now: float | None = None, able: Callable[[str], tuple[bool, str]] | None = None,
          claimer: str = "", healthy: Callable[[str, str, float, str], list[str]] | None = None,
          quiet: Callable[[dict, float, dict], tuple[str, bool]] | None = None,
          budgets: dict | None = None, task_status: Callable[[str, str], str | None] | None = None,
          log: Callable[[str], None] | None = None) -> list[dict]:
    """The pages to deliver now, model-free: [{target, kind, key, alias, stage, id, text}]. Each due
    page is recorded as pending; the pending pages nobody holds (or whose claim lapsed after CLAIM_S)
    are claimed for `claimer` and returned. Mark each `delivered` once it went out, or `release` it.
    `quiet` (default quiet_reason) stops a condition's escalation, or every page of it. `budgets`
    (outagetask.budgets) times the outage task; `task_status` reads its state (outagetask.task_status).
    An overdue entry with nothing to undo is closed instead of paged (auto_close_rule), logged to `log`."""
    now = time.time() if now is None else now
    log = log or (lambda m: None)
    able = able or can_act
    quiet = quiet or quiet_reason
    b = budgets or outagetask.budgets(None)
    task_status = task_status or outagetask.task_status
    if healthy is None:
        from .responsibilities import healthy_checks as healthy
    out: list[dict] = []

    def claim(page: dict) -> None:
        pend = page.get("pending")
        if pend and now - float(pend.get("claimed") or 0) >= CLAIM_S:
            pend.update(claimed=now, claimed_by=claimer)
            out.append({k: v for k, v in pend.items() if k not in ("claimed", "claimed_by")})

    def settle_task(c: dict) -> None:
        # A held or quiet condition escalates no further; a queued task still reaches its project.
        task = c.get("task") or {}
        if (task.get("pending") or {}).get("kind") == outagetask.OVERDUE_KIND:
            task.pop("pending")
        claim(task)

    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        conds = doc.setdefault("conditions", {})
        acts = _read(LEDGER, {"next": 1, "changes": []}).get("changes", [])
        for key, c in list(conds.items()):
            if now - float(c.get("last") or 0) >= CONDITION_GONE_S:
                del conds[key]
                continue
            targets = chain(c["alias"])
            if not targets or now - float(c["first"]) < float(c.get("grace_s", GRACE_S)):
                continue
            page = c.setdefault("page", {})
            held = _vouched(c, targets, now, healthy)
            if held:
                # The owner's own check sees the machine healthy: no recovery page, one low note per episode.
                who, checks = held
                page["held"] = {"by": who, "checks": checks, "at": now}
                if (page.get("pending") or {}).get("kind") == "machine_condition":
                    continue   # a recovery page recorded before the hold stays unclaimed until it lifts
                nid = f"machine:{key}@{int(float(c['first']))}#held"
                why = ("likely the reporting project's own route problem" if c["condition"] == "down"
                       else "the reporting project's reading likely differs or is stale")
                if page.get("noted") != nid and not page.get("pending"):
                    page["noted"] = nid
                    page["pending"] = {
                        "target": who, "kind": "machine_condition_held", "key": f"machine:{key}",
                        "alias": c["alias"], "stage": int(page.get("stage") or 0), "id": nid, "severity": "low",
                        "text": f"Machine {c['alias']} reported {c['condition']} since {ends.shown(c['first'])} by "
                                f"{', '.join(c['seen_by'])}, but your own heal check {', '.join(checks)} sees it "
                                f"healthy: {why}. No recovery page "
                                f"was sent; nothing to do unless you see otherwise."}
                claim(page)
                settle_task(c)
                continue
            page.pop("held", None)
            why, silent = quiet(c, now, doc)
            if why and (silent or "stage" in page):
                # Known, or nobody needs it: no further page, one daily-review record, the user's alert clears.
                page["quiet"] = why
                pend = page.get("pending")
                if pend and pend.get("kind") == "machine_condition" and (silent or int(pend.get("stage") or 0) > 0):
                    page.pop("pending")
                doc.setdefault("quiet", {})[c["alias"]] = {
                    "condition": c["condition"], "first": c["first"], "why": why, "at": now,
                    "seen_by": list(c.get("seen_by") or [])}
                claim(page)
                settle_task(c)
                continue
            if page.pop("quiet", None) is not None and "stage" in page:
                page.update(at=now)   # the steps resume from now, not from the page before the quiet spell
                page.pop("unable_since", None)
            target = _step(page, targets, now, able)
            coordinators = [w for w in c.get("seen_by") or [] if (c.get("escalate_by") or {}).get(w) == "coordinator"]
            if target == USER and coordinators:
                target = coordinators[0]   # its heal check escalates to its coordinator, not the user
            if target:
                # The text is fixed for this step (its duration too): a resend after a crash is the same note.
                _pending(page, target, "machine_condition", f"machine:{key}", c["alias"], float(c["first"]),
                         condition_text(c, page, targets, target, now))
            claim(page)
            if why:
                settle_task(c)   # quiet (paused, a known fault, no work needs it): its owner hears once, no task
                continue
            _task_step(c, key, targets, page, now, able, b, acts, task_status)
            claim(c.get("task") or {})
        doc["quiet"] = {a: q for a, q in (doc.get("quiet") or {}).items()
                        if isinstance(q, dict) and now - float(q.get("at") or 0) < QUIET_KEPT_S}
        _store_outages(doc, outage_entries(now, doc))
        _write(ROUTES, doc)
        ledger = _read(LEDGER, {"next": 1, "changes": []})
        changed, shut = False, []
        for c in ledger.get("changes", []):
            late = overdue(c, now)
            if not late:
                continue
            rule = auto_close_rule(c, now)
            if rule:   # nothing to undo: closed here, under the lock, so once across daemons and restarts
                (c.get("page") or {}).pop("pending", None)
                c.update(closed=now, closed_why=f"auto-closed: {rule}", closed_by=f"{AUTO_CLOSE}: {rule}",
                         auto_closed={"at": now, "rule": rule, "sweeper": claimer})
                changed = True
                shut.append(f"machine change #{c['id']} on {c['alias']} auto-closed ({rule}): {c['what']}")
                continue
            if auto_undo_waits(c, now) and able(str(c.get("by") or ""))[0]:
                # its project's daemon runs the undo (Probes); paged only once that gave up
                if (c.get("page") or {}).pop("pending", None) is not None:
                    changed = True
                continue
            page = c.setdefault("page", {})
            target = _step(page, chain(c["alias"], c.get("by")), now, able)
            changed = True
            gave_up = c.get("auto_undo_failed") or {}
            if target:
                episode = float(c.get("page_from") or c["added"])
                _pending(page, target, "machine_change_overdue", f"machine-change:{c['id']}", c["alias"], episode,
                         f"Machine change #{c['id']} on {c['alias']} is overdue ({late}): {c['what']} "
                         f"(by {c.get('by')}). "
                         + (f"Its auto-undo gave up ({gave_up.get('why')})"
                            + (f"; its output ended: {_clip(str(gave_up['tail'])[-AUTO_UNDO_PAGE_TAIL_CHARS:], AUTO_UNDO_PAGE_TAIL_CHARS)}"
                               if gave_up.get("tail") else "") + ". " if gave_up else "")
                         + f"Page {page['stage'] + 1}: undo it with `{c['undo']}`, "
                         f"then `ttp machines change close {c['id']}`.")
                # Normal unless a live serving machine's change is past its end unconfirmed; one
                # fingerprint per change and episode, so each project hears of it once (pagesev.py).
                page["pending"].update(
                    severity=pagesev.change_severity(c, now, pagesev.serving_machine(machines.load().get(c["alias"]))),
                    fp=pagesev.change_fingerprint(f"machine-change:{c['id']}", episode))
            claim(page)
        if changed:
            _write(LEDGER, ledger)
    for line in shut:   # only once written
        log(line)
    return out


def acting_now(c: dict) -> str:
    """Who is acting on condition `c` now, from its page state (outagemsg wording)."""
    from . import outagemsg
    page = c.get("page") or {}
    targets = chain(c["alias"])
    if not targets:
        return "nobody: no recovery owner set"
    if page.get("held"):
        return f"nobody: {page['held'].get('by')}'s heal check sees it healthy"
    if page.get("quiet"):
        return f"nobody: not escalated ({page['quiet']})"
    if "stage" not in page:
        return f"nobody yet: {targets[0]} is paged once its grace ends"
    stage = int(page["stage"])
    if stage >= len(targets):
        return outagemsg.TO_USER
    m = machines.load().get(c["alias"]) or {}
    who = targets[stage]
    return outagemsg.actor(who, "owner" if who == m.get("recovery_owner") else
                           "fallback" if who == m.get("recovery_fallback") else "",
                           # `why` outlives a step (the next page names it); `unable_since` is this target's
                           f"cannot act yet: {page['why']}" if page.get("why") and page.get("unable_since") else "")


def condition_line(c: dict, now: float, where=None, so_far: bool = True) -> str:
    """'down since <start> (<duration> so far; seen by ...): <text>. Acting now: <who>[. Tracked task: #N]'.
    `so_far` False leaves out the duration (the coordinator digest, which would else reword it each turn)."""
    from . import outagemsg
    page = c.get("page") or {}
    lasted = f"{outagemsg.duration(now - float(c['first']))} so far; " if so_far else ""
    task = tracked(c)[0]
    tid = outagemsg._int(task)
    name = f"#{tid}" if tid else task if isinstance(task, str) else ""
    return (f"{c['condition']} since {ends.shown(c['first'], where)} ({lasted}seen by "
            f"{', '.join(c.get('seen_by') or [])}): {c.get('text') or 'no detail'}. "
            f"{outagemsg.ACTING} {acting_now(c)}" + (f". Tracked task: {name}" if name else ""))


def tracked(c: dict) -> tuple[Any, Any]:
    """(task, due) a condition's messages name: its outage task (outagetask.py) as '<label> in <project>',
    else a task id and due time a page or the condition records."""
    t, page = c.get("task"), c.get("page") or {}
    if isinstance(t, dict):
        return f"{t.get('label')} in {t.get('project')}", t.get("due")
    return page.get("task") or t, page.get("due") or c.get("due")


def condition_text(c: dict, page: dict, targets: list[str], target: str, now: float) -> str:
    """A machine_condition page's text for this step (outagemsg.facts): since when and how long, who is
    acting now (the target, its role, and who was passed over and why), the tracked task and due time
    when the page or condition records one, the pages already sent, and the next step."""
    from . import outagemsg
    stage = int(page.get("stage") or 0)
    m = machines.load().get(c["alias"]) or {}
    passed = f"{targets[stage - 1]} cannot act: {page.get('why') or 'no answer'}" if 0 < stage <= len(targets) else ""
    if target == USER:
        acting = outagemsg.actor(None, passed=passed)
        step = (f"the user checks and recovers {c['alias']}, or names a project that can "
                f"(`ttp machines set {c['alias']} --owner <project>`)")
    else:
        role = ("owner" if target == m.get("recovery_owner") else "fallback" if target == m.get("recovery_fallback")
                else "coordinator of the reporting project" if target in (c.get("seen_by") or []) else "")
        acting = outagemsg.actor(target, role, passed)
        step = f"{target} checks and recovers it; record any live change with `ttp machines change add`"
    tried = [f"recovery page {i + 1} to {t}" for i, t in enumerate(targets[:stage])]
    task, due = tracked(c)
    return (f"Machine {c['alias']} {c['condition']}, seen by {', '.join(c.get('seen_by') or [])}: "
            f"{c.get('text') or 'no detail'}. Recovery page {stage + 1}. "
            + outagemsg.facts(float(c["first"]), now, acting, task=task, due=due, tried=tried,
                              next_step=step))


def _settle(sent: dict, done: bool) -> bool:
    """Mark the pending page `sent` delivered (done) or free to retry; False when it is no longer pending."""
    with _locked():
        for name, empty in ((ROUTES, {"conditions": {}}), (LEDGER, {"next": 1, "changes": []})):
            doc = _read(name, empty)
            items = doc.get("conditions", {}).values() if name == ROUTES else doc.get("changes", [])
            slots = [c.get(s) for c in items if isinstance(c, dict) for s in ("page", "task")]
            for slot in slots:
                pend = slot.get("pending") if isinstance(slot, dict) else None
                if not pend or pend.get("id") != sent.get("id"):
                    continue
                if done:
                    slot.pop("pending")
                    slot["delivered"] = sent["id"]
                else:
                    pend.pop("claimed", None)
                    pend.pop("claimed_by", None)
                _write(name, doc)
                return True
    return False


def delivered(page: dict) -> bool:
    """Page `page` (from `route`) went out: it is not sent again."""
    return _settle(page, True)


def release(page: dict) -> bool:
    """Page `page` (from `route`) could not go out: the next tick retries it."""
    return _settle(page, False)


def condition_open(key: str, now: float | None = None) -> bool:
    """Whether condition `key` (alias:condition) is still reported (alerts.holds)."""
    now = time.time() if now is None else now
    c = conditions().get(key)
    return isinstance(c, dict) and now - float(c.get("last") or 0) < CONDITION_GONE_S \
        and not (c.get("page") or {}).get("quiet")


def change_overdue(cid: int, now: float | None = None) -> bool:
    """Whether change #cid is open and overdue (alerts.holds)."""
    return any(c.get("id") == cid and overdue(c, now) for c in changes())


# probes -------------------------------------------------------------------------------------------

PROBE_BROKEN_KIND = "change_probe_broken"   # the event a broken until-probe queues for the coordinator
TRIVIAL_CODE = "always exits 0"   # how a stored until-probe that always exits 0 is reported broken


class Probes:
    """Runs the until-probes of the entries project `p` made, in the background, from its root."""

    def __init__(self, p, log: Callable[[str], None] = print):
        self.p, self.log = p, log
        self._procs: dict[int, tuple[subprocess.Popen, float, str]] = {}
        self._probed: dict[int, float] = {}
        self._errs: dict[int, Any] = {}   # id(proc) -> its stderr file
        self._reported: dict[int, str] = {}   # change id -> the broken kind ("exit 2", "timeout") already reported
        self._passes: dict[int, tuple[str, float, int]] = {}   # change id -> (probe, set at, its passes in a row)
        self._undos: dict[int, tuple[subprocess.Popen, float, Any, dict]] = {}   # id -> (proc, started, output, claim)
        self._undo_waits: dict[int, str] = {}   # id -> why its auto-undo waits (a pause, an outage), logged once

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        try:
            if getattr(self.p, "state", None):
                apply_requests(self.p.state, self.log)
        except OSError as e:   # kept queued for the next tick
            self.log(f"queued machine-ledger requests not applied yet: {e}")
        passed, unpassed = {}, set()
        started = {cid: v[1] for cid, v in self._procs.items()}
        since = {c.get("id"): float(c.get("page_from") or c.get("added") or 0) for c in changes()}
        for cid, (rc, probe, err) in ends.reap_probes(self._procs, self._errs, now, self.p).items():
            if started.get(cid, now) < since.get(cid, 0):   # began before its probe was (re)set: says nothing of it
                self._passes.pop(cid, None)
                continue
            if rc == 0 or (rc not in ends.NOT_YET_RCS and detach.failed_check(probe, rc)):   # the jobs it waits on ended, even if one failed
                self._reported.pop(cid, None)
                was = self._passes.get(cid, ("", 0.0, 0))
                n = was[2] + 1 if was[:2] == (probe, since.get(cid, 0)) else 1
                self._passes[cid] = (probe, since.get(cid, 0), n)
                if n <= PROBE_PASSES:   # not every round while the entry stays open overdue
                    self.log(f"machine change #{cid}: its until-probe passed ({n} of {PROBE_PASSES} in a row; "
                             f"exit {rc}): {(err or 'no output')[-300:]}")
                if n >= PROBE_PASSES:
                    passed[(cid, probe)] = (rc, err)
                continue
            self._passes.pop(cid, None)
            if rc in ends.NOT_YET_RCS:   # 255 only once ssh cannot reach the host it names: says nothing either way
                self._reported.pop(cid, None)
                if rc != netwait.UNREACHABLE_RC:
                    unpassed.add((cid, probe))
            else:   # broken, a timeout too (as ends.EndProbes reads it): said once per kind, not every round
                self._broken(cid, probe, rc, err, now)
        if passed or any((c.get("id"), c.get("probe")) in unpassed and c.get("probe_passed") for c in changes()):
            self._mark(passed, unpassed, now)
        self._tasks(now)
        self._auto_undos(now)
        for c in changes():
            if c.get("kind") == POWER_CYCLE and float(c.get("expires") or 0) <= now:   # any daemon: the maker's may be gone
                with contextlib.suppress(ValueError):   # another daemon closed it first
                    close_change(int(c["id"]), "the planned power cycle is past", self.p.name, now)
        open_ids = set()
        for c in changes():
            cid = c.get("id")
            open_ids.add(cid)
            if c.get("by") != self.p.name or not c.get("probe") or cid in self._procs \
                    or now - float(c.get("page_from") or c.get("added") or 0) < PROBE_GRACE_S \
                    or now - self._probed.get(cid, 0) < ends.PROBE_EVERY_S:
                continue
            self._probed[cid] = now
            if trivial_probe(c):   # stored before add and edit refused it: never run, so it never ends the entry
                self._broken(cid, c["probe"], TRIVIAL_CODE, "it is or ends in `true`, `:` or `exit 0`, so its pass "
                             "would say nothing; end the test with `|| exit 1` instead", now)
                continue
            try:
                self._procs[cid] = (ends.spawn_probe(self.p, c["probe"], self._errs, stdout=True), now, c["probe"])
            except OSError as e:
                self.log(f"machine change #{cid}: until-probe could not start: {e}")
        for cid in set(self._passes) - open_ids:   # closed: its streak is over
            del self._passes[cid]

    def _mark(self, passed: dict, unpassed: set, now: float) -> None:
        """Record the until-probes that passed PROBE_PASSES times in a row ({(id, probe): (rc, output tail)}),
        and clear the pass and its pages of those that said "not yet" since (`unpassed`): a pass an edit's
        probe replaced is not the entry's."""
        with _locked():
            doc = _read(LEDGER, {"next": 1, "changes": []})
            for c in doc.get("changes", []):
                key = (c.get("id"), c.get("probe"))
                if c.get("closed"):
                    continue
                if key in passed and not c.get("probe_passed"):
                    rc, out = passed[key]
                    c.update(probe_passed=now, probe_out=out[-300:])
                    if rc:
                        c["probe_job_failed"] = detach.failed_why(c["probe"], rc, self.p.root)
                elif key in unpassed and c.get("probe_passed"):
                    self.log(f"machine change #{c['id']}: its until-probe passed before but says not yet now: "
                             f"no longer overdue")
                    for k in ("probe_passed", "probe_job_failed", "probe_out", "auto_undo_next", "auto_undo_tries"):
                        c.pop(k, None)   # a later pass starts a fresh auto-undo episode too
                    if c.pop("page", None) is not None:
                        c["page_from"] = now   # a later pass starts a new page episode
            _write(LEDGER, doc)

    def _tasks(self, now: float) -> None:
        """Mark this project's open entries whose until-task is over (task_ended), so any daemon's route
        pages it as overdue."""
        over = {(c["id"], c["until_task"]): why for c in changes() if c.get("by") == self.p.name
                and c.get("until_task") and not c.get("task_ended") and (why := task_over(self.p, c["until_task"]))}
        if not over:
            return
        status = {}   # the task's end when it resolved to one (auto_close_rule); none when deleted or unknown
        for _, tid in over:
            row = self.p.db.task(int(tid)) if str(tid).isdigit() else None
            if row is not None and row["status"] in TERMINAL_TASK_STATES:
                status[tid] = row["status"]
        with _locked():
            doc = _read(LEDGER, {"next": 1, "changes": []})
            for c in doc.get("changes", []):   # not an until-task an edit replaced meanwhile
                if (c.get("id"), c.get("until_task")) in over and not c.get("closed") and not c.get("task_ended"):
                    c.update(task_ended=now, task_end_why=over[(c["id"], c["until_task"])],
                             **({"task_status": status[c["until_task"]]} if c["until_task"] in status else {}))
            _write(LEDGER, doc)

    def _auto_undos(self, now: float) -> None:
        """Run the undo of this project's --auto-undo entries once due (module doc: Auto-undo): reap the runs
        that ended or ran past AUTO_UNDO_TIMEOUT_S (their whole group killed), settle a claim no run of this
        daemon holds (a restart interrupted it) without running it again, then claim and start the due ones
        whose machine is not paused or out."""
        for cid, (proc, started, out, claim) in list(self._undos.items()):
            rc = proc.poll()
            if rc is None and now - started < AUTO_UNDO_TIMEOUT_S:
                continue
            del self._undos[cid]
            if rc is None:
                _kill_group(proc)
            self._settle_undo(cid, claim, "timeout" if rc is None else rc, _tail(out), now)
        for c in changes():
            claim = c.get("auto_undo_claim")
            if c.get("by") == self.p.name and isinstance(claim, dict) and c.get("id") not in self._undos:
                self._settle_undo(int(c["id"]), claim, "interrupted", "", now)
        for c in changes():
            cid = c.get("id")
            if c.get("by") != self.p.name or cid in self._undos or not auto_undo_due(c, now):
                continue
            why = auto_undo_paused(c, self.p, now)
            if why:   # waits, unpaged, until the pause or outage ends
                if self._undo_waits.get(cid) != why:
                    self._undo_waits[cid] = why
                    self.log(f"machine change #{cid}: its auto-undo waits: {why}")
                continue
            self._undo_waits.pop(cid, None)
            claim = self._claim_undo(int(cid), now)
            if not claim:
                continue
            out = tempfile.TemporaryFile()
            try:   # its own session and process group, never the daemon's; no stdin
                proc = subprocess.Popen(claim["undo"], shell=True, cwd=str(self.p.root),
                                        env=project.command_env(self.p), stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as e:
                out.close()
                self._settle_undo(int(cid), claim, "could not start", str(e), now)
                continue
            self._undos[int(cid)] = (proc, now, out, claim)
            self.log(f"machine change #{cid}: auto-undo try {claim['try']} of {AUTO_UNDO_TRIES} started "
                     f"(pid {proc.pid}): {claim['undo'][:200]}")

    def _claim_undo(self, cid: int, now: float) -> dict | None:
        """Record the run before it starts, under the lock, so a restart never runs it twice: the claim, or
        None when the entry is no longer due or another run holds it."""
        with _locked():
            doc = _read(LEDGER, {"next": 1, "changes": []})
            c = next((c for c in doc.get("changes", []) if isinstance(c, dict) and c.get("id") == cid), None)
            if c is None or c.get("auto_undo_claim") or not auto_undo_due(c, now):
                return None
            n = int(c.get("auto_undo_tries") or 0) + 1
            claim = {"at": now, "by": self.p.name, "daemon_pid": os.getpid(), "try": n, "undo": c["undo"],
                     "token": os.urandom(6).hex()}
            c.update(auto_undo_claim=claim, auto_undo_tries=n)
            c.pop("auto_undo_next", None)
            _write(LEDGER, doc)
        return claim

    def _settle_undo(self, cid: int, claim: dict, rc, tail: str, now: float) -> None:
        """Record one run's outcome in the entry's history (auto_undo_runs) and act on it: exit 0 closes the
        entry; 1, 75 or 255 retries after PROBE_GRACE_S up to AUTO_UNDO_TRIES runs; anything else (a timeout,
        a failed start, a run a restart interrupted) gives up, which route pages with the tail."""
        tail = str(tail or "")[-AUTO_UNDO_TAIL_CHARS:]
        n = int(claim.get("try") or 1)
        retry = rc in AUTO_UNDO_RETRY_RCS and n < AUTO_UNDO_TRIES
        code = rc if isinstance(rc, str) else f"exit {rc}"
        if rc == 0:
            outcome = "ok"
        elif retry:
            outcome = f"{code}; retry after {PROBE_GRACE_S}s"
        elif rc in AUTO_UNDO_RETRY_RCS:
            outcome = f"gave up: {code} on each of {AUTO_UNDO_TRIES} tries"
        elif rc == "timeout":
            outcome = f"gave up: timed out after {AUTO_UNDO_TIMEOUT_S}s (its process group killed)"
        elif rc == "interrupted":
            outcome = (f"gave up: a daemon restart interrupted its run, so it is not run again (claim: try {n}, "
                       f"{ends.shown(float(claim.get('at') or now), self.p)} by {claim.get('by')}, daemon pid "
                       f"{claim.get('daemon_pid')})")
        else:
            outcome = f"gave up: {code}"
        with _locked():
            doc = _read(LEDGER, {"next": 1, "changes": []})
            c = next((c for c in doc.get("changes", []) if isinstance(c, dict) and c.get("id") == cid), None)
            if c is None or (c.get("auto_undo_claim") or {}).get("token") != claim.get("token"):
                return   # settled already, or the entry is gone
            c.pop("auto_undo_claim", None)
            run = {"at": claim.get("at"), "ended": now, "try": n, "rc": rc, "outcome": outcome, "tail": tail}
            c["auto_undo_runs"] = (list(c.get("auto_undo_runs") or []) + [run])[-AUTO_UNDO_RUNS_KEPT:]
            if rc == 0:
                (c.get("page") or {}).pop("pending", None)
                if not c.get("closed"):   # one closed by hand during the run keeps who closed it
                    c.update(closed=now, closed_why="auto-undo ok", closed_by=AUTO_UNDO)
                c["auto_undone"] = {"at": now, "try": n}
            elif retry:
                c["auto_undo_next"] = now + PROBE_GRACE_S
            else:
                c["auto_undo_failed"] = {"at": now, "why": outcome.removeprefix("gave up: "), "tail": tail}
            _write(LEDGER, doc)
        self.log(f"machine change #{cid}: auto-undo try {n}: {outcome}" + (f": {tail[-300:]}" if tail else ""))

    def _broken(self, cid: int, probe: str, rc, err: str, now: float) -> None:
        """Log a broken until-probe and queue one low event for the coordinator, keyed on the change, the
        probe and how it broke (its exit code or "timeout"), never its stderr: once across restarts too."""
        code = rc if isinstance(rc, str) else f"exit {rc}"
        if self._reported.get(cid) == code:
            return
        self._reported[cid] = code
        why = f"{code}: {err}" if err else code
        self.log(f"machine change #{cid}: its until-probe is broken ({why[-500:]})")
        fp = f"{PROBE_BROKEN_KIND}:{cid}:{hashlib.sha256(probe.encode()).hexdigest()[:12]}:{code}"
        db = self.p.db
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            return
        c = next((x for x in changes() if x.get("id") == cid), {})
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (now, "daemon", PROBE_BROKEN_KIND, fp, "low",
              f"Machine change #{cid} on {c.get('alias', '?')} ({str(c.get('what', ''))[:200]}) ends when its "
              f"until-probe passes, but the probe is broken ({why[-300:]}; only 0 = over and 1, 75 or 255 = not "
              f"yet are valid, and it must finish within {ends.PROBE_TIMEOUT_S}s): {probe[:300]}. Unfixed, it is "
              f"flagged only once overdue ({ends.shown(float(c.get('expires') or now), self.p)}). Fix it: `ttp machines change edit {cid} --until-probe <command>`, or close "
              f"the change once it is undone.", "queued", c.get("task")))

    def stop(self) -> None:
        ends.stop_probes(self._procs, self._errs)
        for cid, (proc, _, out, claim) in list(self._undos.items()):   # nothing runs unobserved: paged as interrupted
            del self._undos[cid]
            _kill_group(proc)
            with contextlib.suppress(Exception):
                self._settle_undo(cid, claim, "interrupted", _tail(out), time.time())


def _kill_group(proc: subprocess.Popen) -> None:
    """End an auto-undo and whatever it started: its own process group, never the daemon's."""
    try:
        pgid = os.getpgid(proc.pid)
        if pgid != os.getpgrp():
            os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        proc.wait(timeout=5)


def _tail(out: Any) -> str:
    """The last AUTO_UNDO_TAIL_CHARS of a run's output file, which is then closed."""
    try:
        out.seek(0, os.SEEK_END)
        out.seek(max(0, out.tell() - 4 * AUTO_UNDO_TAIL_CHARS))
        return out.read().decode("utf-8", "replace")[-AUTO_UNDO_TAIL_CHARS:]
    except (OSError, ValueError):
        return ""
    finally:
        with contextlib.suppress(OSError):
            out.close()


# digest -------------------------------------------------------------------------------------------

# A clause that refuses a machine ("No authorization for <alias>", "never <alias>", "<alias> is
# excluded") does not make it the project's. A negation excludes an alias only where it governs it: it
# comes before the alias with only a list of aliases between its phrase and the alias, or the alias is
# the subject of a prohibition. One that limits how a machine is used ("box-a, never directly", "no more
# than 2 at a time on box-a", "use box-a without sudo", "box-a: do not modify") keeps it.
NEGATION_RE = re.compile(r"\b(?:no|not|never|none|nor|without|excluded?|exclusions?|prohibited|forbidden|"
                         r"off[- ]limits|disallowed|unauthori[sz]ed)\b|n't\b", re.I)
LIMIT_RE = re.compile(r"\b(?:no|not)\s+(?:more|less|fewer|longer|later|earlier|sooner|greater|higher|lower|"
                      r"over)\s+than\b|\bnot\s+only\b|\bno\s+matter\b", re.I)
PROHIBITED_RE = re.compile(r"\s*(?:[:\u2013\u2014-]|\b(?:is|are|stays?|remains?)\b)?\s*(?:strictly\s+|also\s+)?"
                           r"(?:excluded|prohibited|forbidden|off[- ]limits|disallowed|unauthori[sz]ed|out of scope|"
                           r"not\s+(?:allowed|authori[sz]ed|permitted|ours|in scope|to be used))\b", re.I)
# A negation followed only by a maintenance verb ("Do not reboot box-a") limits an action; it does not
# refuse the machine. Access verbs (use, run on, access, touch, take) still do.
MAINT_RE = re.compile(r"^\s*(?:(?:allowed|permitted|authori[sz]ed)\s+)?(?:to\s+)?(?:(?:(?:re)?boot|reset|restart|"
                      r"modify|upgrade|power[- ]cycle)\b\s*(?:,|\band\b|\bor\b)?\s*)+$", re.I)
GLUE_RE = re.compile(r"^\s*(?:(?:and|or|nor|the|a)\b\s*)*$", re.I)
CLAUSE_RE = re.compile(r"[.;!?](?=\s|$)|\n|\(|\)")


def _refused(clause: str, start: int, end: int, alias_re) -> bool:
    """Whether the alias at clause[start:end] is the one a negation in the clause refuses."""
    after = alias_re.sub(" ", clause[end:])
    if PROHIBITED_RE.match(re.sub(r"^(?:\s*(?:,|\band\b|\bor\b)\s*)+", "", after)):
        return True
    before = LIMIT_RE.sub(lambda m: "_" * len(m.group()), clause[:start])
    neg = None
    for neg in NEGATION_RE.finditer(before):
        pass
    if not neg:
        return False
    between = before[neg.end():]
    if neg.group().lower() == "without":
        return bool(GLUE_RE.match(alias_re.sub(" ", between)))
    if MAINT_RE.match(between):
        return False
    # The negation's own phrase ends at a comma, colon or dash; past it, only a list of aliases continues it.
    *rest, own = re.split(r"[,:\u2013\u2014]|\s-\s", between)
    return not rest or all(GLUE_RE.match(alias_re.sub(" ", seg)) for seg in rest[1:] + [own])


# "Out of service" is left out: it is usually temporary ("until Monday", "for maintenance").
RETIRED_WORDS = r"(?:retired|decommissioned|do(?:n't| not)\s+use)\b"
# "box-a is retired", "box-a (decommissioned)", "box-a: do not use": the words right after the alias.
RETIRED_AFTER_RE = re.compile(r"\s*\(?\s*(?:(?:[:\u2013\u2014-]|\b(?:is|are|was|were|has been|have been|now|stays?|"
                              rf"remains?)\b)\s*)*{RETIRED_WORDS}", re.I)
# "box-a and box-b are retired": the words after a list of aliases, which a plural verb makes all of them.
RETIRED_LIST_RE = re.compile(rf"\s*(?:are|were|have been|remain|stay)\s+(?:now\s+)?{RETIRED_WORDS}", re.I)
# "Retired: box-a, box-b", "Decommissioned box-a": the words before the alias, past only a list of aliases.
RETIRED_BEFORE_RE = re.compile(rf"{RETIRED_WORDS}(?:\s*[:\u2013\u2014,-]?\s*|\s+(?:and|or|the)\s+)*$", re.I)
RETIRED_CLAUSE_RE = re.compile(r"[.;!?](?=\s|$)|\n")


def _retires(word: str, ahead: str, prior: str) -> bool:
    """Whether retirement word `word` (a RETIRED_WORDS match) retires its alias: not negated or still to come
    (`prior`, the clause before it), and a "do not use" only when bare (`ahead`, what follows it and the
    aliases it names): "do not use for builds", "Do not use box-a while ..." only limit its use."""
    if machines.NOT_YET_RE.search(prior):
        return False
    return not machines.DO_NOT_USE_RE.match(word) or bool(machines.BARE_USE_RE.match(ahead))


def charter_retired(text: str, known) -> set[str]:
    """The machines in `known` that Resources text `text` calls retired, decommissioned or a bare "do not
    use", in the same sentence as the alias (an older paragraph naming it as a resource does not bring it
    back)."""
    pat = {a: re.compile(rf"(?<![\w.-]){re.escape(a)}(?![\w-]|\.\w)") for a in known}
    any_alias = re.compile("|".join(f"(?:{r.pattern})" for r in pat.values()) or r"(?!)")
    word = re.compile(RETIRED_WORDS, re.I)
    out = set()
    for c in RETIRED_CLAUSE_RE.split(text):
        for a, r in pat.items():
            for m in r.finditer(c):
                listed = re.sub(r"^(?:\s*(?:,|\band\b|\bor\b)\s*)+", "", any_alias.sub(" ", c[m.end():]))
                before = any_alias.sub(" ", c[:m.start()])
                before = re.sub(r"(?:\s*(?:,|\band\b|\bor\b)\s*)+$", "", before)
                hits = []
                if x := RETIRED_AFTER_RE.match(c[m.end():]):
                    w = word.search(x.group())
                    hits.append((w.group(), c[m.end() + x.end():], c[:m.end() + w.start()]))
                if x := RETIRED_LIST_RE.match(listed):
                    w = word.search(x.group())
                    hits.append((w.group(), listed[x.end():], listed[:w.start()]))
                if x := RETIRED_BEFORE_RE.search(before):
                    w = word.match(before, x.start())
                    hits.append((w.group(), listed, before[:x.start()]))
                if any(_retires(*h) for h in hits):
                    out.add(a)
    return out


def charter_machines(charter: str, known) -> list[str]:
    """The machines in `known` that the charter's Resources section names somewhere no negation refuses,
    less those it calls retired (charter_retired)."""
    from .prompts import charter_sections
    text = "\n".join("\n".join(body) for head, body in charter_sections(charter)
                     if ends.section_name(head).lower().startswith("resources"))
    clauses = [c for c in CLAUSE_RE.split(text) if c.strip()]
    pat = {a: re.compile(rf"(?<![\w.-]){re.escape(a)}(?![\w-]|\.\w)") for a in known}
    any_alias = re.compile("|".join(f"(?:{r.pattern})" for r in pat.values()) or r"(?!)")
    gone = charter_retired(text, known)
    return sorted(a for a in known if a not in gone and any(not _refused(c, m.start(), m.end(), any_alias)
                                                            for c in clauses for m in pat[a].finditer(c)))


def configured_machines(cfg: dict, known) -> list[str]:
    """The machines in `known` the project's config authorizes explicitly: a `resources` limit or a
    `shared_resources` entry that is the alias or a resource on it (`<alias>-device`)."""
    res = cfg.get("resources")
    shared = cfg.get("shared_resources")
    names = [str(x) for x in (res if isinstance(res, dict) else {})]
    names += [x for x in (shared if isinstance(shared, list) else []) if isinstance(x, str)]
    return sorted(a for a in known if any(n == a or n.startswith((a + "-", a + ":")) for n in names))


def project_machines(charter: str, cfg: dict, known) -> list[str]:
    """A project's machines: those its config authorizes explicitly when it names any, else those its
    charter's Resources section names outside a prohibition. A machine whose list entry is retired
    (machines.retired) is never one."""
    live = [a for a in known if not machines.retired(known.get(a) if isinstance(known, dict) else None)]
    return configured_machines(cfg, live) or charter_machines(charter, live)


def resource_machines(p) -> list[str]:
    """The machines in the user's list that are the project's (project_machines)."""
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    try:
        cfg = p.raw_config()
    except Exception:
        cfg = {}
    return project_machines(charter, cfg if isinstance(cfg, dict) else {}, machines.load())


def digest_lines(p, now: float | None = None) -> list[str]:
    """The open changes and conditions on the machines in `p`'s Resources, one line each, and each
    machine there without a recovery owner, named once per project."""
    return [line for _, line in digest_items(p, now)]


GAP_ITEM = "no_owner"   # digest_items' key of the line naming machines without a recovery owner


def _ends_itself(c: dict, p, now: float) -> bool:
    """Whether open entry `c` is another project's that ends by itself and needs nothing from `p`: a
    power-cycle intent, or a change with a set end time (no until-probe) that is not overdue yet. Its author
    is paged once it is overdue, and then it shows in full again."""
    return (c.get("by") != getattr(p, "name", None) and not overdue(c, now)
            and not c.get("probe"))


def digest_items(p, now: float | None = None) -> list[tuple[str, str]]:
    """digest_lines as (stable key, line): a change by its id, a condition by alias and condition, an
    outage by alias, so a reworded line keeps its key (the digest shows only new or reworded lines).
    Changes are clipped (DIGEST_LINE_CHARS); other projects' that end by themselves are one count per
    machine. Conditions and outages show in full."""
    now = time.time() if now is None else now
    mine = resource_machines(p)
    if not mine:
        return []
    known = machines.load()
    items, quiet = [], {}
    for c in changes(set(mine)):
        if _ends_itself(c, p, now):
            quiet[c["alias"]] = quiet.get(c["alias"], 0) + 1
        else:
            items.append((f"change:{c.get('id')}", change_line(c, now, p, clip=DIGEST_LINE_CHARS)))
    items += [(f"others:{a}", f"{a}: {n} more open change{'s' if n > 1 else ''} by other projects that end by "
               f"themselves (`ttp machines change list {a}`)") for a, n in sorted(quiet.items())]
    for key, c in conditions().items():
        if c.get("alias") in mine:
            owner = (known.get(c["alias"]) or {}).get("recovery_owner")
            held = (c.get("page") or {}).get("held")
            hush = (c.get("page") or {}).get("quiet")
            items.append((f"condition:{key}",
                          f"{c['alias']} {c['condition']} since {ends.shown(c['first'], p)} (seen by "
                          f"{', '.join(c.get('seen_by', []))}; recovery owner: {owner or 'none set'}"
                          + (f"; page held: {held['by']}'s heal check sees it healthy" if held else "")
                          + (f"; not escalated: {hush}" if hush else "")
                          + (f"; acting now: {acting_now(c)}" if not held and not hush else "") + ")"))
    items += [(f"outage:{a}", outage_line(a, o, p)) for a, o in sorted(outages(now).items()) if a in mine]
    named = list(p.db.kv(GAP_KV) or [])
    gaps = [a for a in mine if not (known.get(a) or {}).get("recovery_owner") and a not in named]
    if gaps:
        items.append((GAP_ITEM, "No recovery owner set for " + ", ".join(gaps) + ": a long down or held condition "
                      "there reaches nobody. Set one: `ttp machines set <alias> --owner <project> "
                      "[--fallback <project>]` (named once)."))
        p.db.set_kv(GAP_KV, named + gaps)
    return items


def review_lines(p, now: float | None = None) -> list[str]:
    """For the daily review: each outage of the last day on a machine in `p`'s Resources (or one it is the
    recovery owner or fallback of) that was not paged, and why; one line per machine."""
    now = time.time() if now is None else now
    known = machines.load()
    mine = set(resource_machines(p)) | {a for a, m in known.items()
                                        if p.name in (m.get("recovery_owner"), m.get("recovery_fallback"))}
    got = _read(ROUTES, {"conditions": {}}).get("quiet") or {}
    return [f"{a} {q.get('condition')} since {ends.shown(float(q.get('first') or 0), p)} (seen by "
            f"{', '.join(q.get('seen_by') or []) or '?'}), last {ends.shown(float(q['at']), p)}: not paged: {q.get('why')}"
            for a, q in sorted(got.items()) if a in mine and isinstance(q, dict)
            and now - float(q.get("at") or 0) < REVIEW_S]


def task_lines(p) -> list[str]:
    """For the daily review: each open entry project `p` made whose until-task is over (done, failed,
    cancelled or gone) and that nobody undid and closed yet; one line each."""
    return [f"{change_line(c, where=p)} (task over: {why}; undo it and `ttp machines change close {c['id']}`, or "
            f"re-extend it for a continuation: `ttp machines change edit {c['id']} --until-task <id>`)"
            for c in changes() if c.get("by") == p.name and c.get("until_task")
            and (why := task_over(p, c["until_task"]))]
