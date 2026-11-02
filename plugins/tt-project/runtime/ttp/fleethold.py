# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Fleet holds: one machine-wide device hold that every project of the user on this host honours.

`ttp pause --machine <alias>` (or the coordinator's `resource_pause` with `machine`) stores one hold
per machine alias in ~/.tt-project/fleet-holds.json. Every project folds the active holds into its
paused resources (db.paused_resources, through shared.paused_for_db): each resource name the project
uses that is on the machine (machine_ledger.on_machine: the alias, `<alias>-x` names, its hostname)
is paused there, marked {"fleet": <hold id>, "project": <setter>}. So dispatch, `ttp lock`, status
and the web app honour it with no pause of their own, and each daemon tells its workers and wakes
its waiters when a hold starts or ends (coordinator.sync_shared_pauses).

A hold set by the user (`ttp pause --machine` outside a run, or the coordinator acting on the user's
own words in its turn) is lifted only on their word: `ttp resume --machine` outside a run, or a
`resource_pause` with `machine` and paused false in a turn of any project that carries the user's
own message. Its end only reports: once it passed, each project with queued tasks on the machine
shows one 'needs you' alert (passed_alerts) until the hold is lifted or extended. Any project's coordinator may lift a coordinator's hold; the
setter is told. The setter's daemon checks the hold's end (pauseends); when the setter cannot act,
the machine's recovery owner does. A pause asked for a resource a hold already covers is a no-op.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import project

FILE = "fleet-holds.json"
LOCK = "fleet-holds.lock"
END_FIELDS = ("until", "end_when", "report_from", "user_end")
KEPT_S = 30 * 86400   # lifted holds are kept this long, for status and audit
ALERT_KIND = "fleethold"   # alert `fleethold:<id>:passed`: a user's hold past its expected end (passed_alerts)
LIFT = "to lift it, say so in chat or use Resume on the web page"
PASSED = "passed"   # on a hold: {"sig", "at"}, when its end condition passed where it is checked (record_passed)


def _path(name: str) -> Path:
    return project.HOME_DIR / name


@contextlib.contextmanager
def _locked():
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(_path(LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read() -> dict:
    try:
        doc = json.loads(_path(FILE).read_text())
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("holds"), dict):
        return {"next": 1, "holds": {}, "lifted": []}
    doc.setdefault("next", 1)
    doc["lifted"] = [x for x in doc.get("lifted") or [] if isinstance(x, dict)]
    return doc


def active() -> dict[str, dict]:
    """The holds in force, by machine alias. Read without the lock: the file is replaced whole."""
    return {k: v for k, v in _read()["holds"].items() if isinstance(v, dict)}


def _known(alias: str) -> None:
    from . import machines
    if alias not in machines.load():
        raise ValueError(f"{alias!r} is not in the machines list (`ttp machines list`); a fleet hold names a "
                         f"machine alias")


def set_hold(alias: str, reason: str, by: str, setter: str, end: dict | None = None,
             user_msg: Any = None, now: float | None = None) -> tuple[dict, bool]:
    """Hold machine `alias` for every project. Returns (the hold, whether it is new). A hold already
    in force keeps its id and start; a re-hold replaces its reason and end, and stays the user's
    when the user had any part in it."""
    alias = (alias or "").strip()
    _known(alias)
    if by not in ("user", "coordinator", "worker"):
        raise ValueError(f"a fleet hold is by the user, a coordinator or a worker, not {by!r}")
    now = time.time() if now is None else now
    reason = " ".join(str(reason or "").split())[:300]
    with _locked():
        doc = _read()
        was = doc["holds"].get(alias)
        if isinstance(was, dict):
            kept = end if end is not None else {k: was[k] for k in END_FIELDS if k in was}
            hold = {**{k: v for k, v in was.items() if k not in END_FIELDS and k != PASSED}, **kept,
                    "reason": reason or was.get("reason", ""), "generation": uuid.uuid4().hex,
                    "by": "user" if "user" in (by, was.get("by")) else by,
                    "edits": int(was.get("edits") or 0) + 1}
            if user_msg is not None:
                hold["user_msg"] = user_msg
            new = False
        else:
            hold = {"id": int(doc["next"]), "alias": alias, "reason": reason, "by": by, "project": setter,
                    "since": now, "generation": uuid.uuid4().hex, "edits": 0, **(end or {}),
                    **({"user_msg": user_msg} if user_msg is not None else {})}
            doc["next"] = int(doc["next"]) + 1
            new = True
        doc["holds"][alias] = hold
        project.write_json(_path(FILE), doc, 0o600)
    return hold, new


def lift(alias: str, users_word: bool, by: str, now: float | None = None) -> dict | None:
    """Lift the hold of `alias`; None when there is none. A hold the user set needs `users_word`."""
    now = time.time() if now is None else now
    with _locked():
        doc = _read()
        hold = doc["holds"].get(alias)
        if not isinstance(hold, dict):
            return None
        if hold.get("by") == "user" and not users_word:
            raise ValueError(f"{alias} is held machine-wide for the user (hold {hold.get('id')}); only the user "
                             f"lifts it: `ttp resume --machine {alias}` outside a run, or in a turn that carries "
                             f"their own words")
        _drop(doc, alias, {"lifted": now, "lifted_by": by}, now)
    return hold


def lift_at_user_end(alias: str, generation: Any, by: str, now: float | None = None) -> dict | None:
    """Lift the user's hold of `alias` at the end their own words gave (`user_end`, pauseends): only the
    hold of that `generation`, once its `until` passed. None when there is no such hold."""
    now = time.time() if now is None else now
    with _locked():
        doc = _read()
        hold = doc["holds"].get(alias)
        if not (isinstance(hold, dict) and hold.get("generation") == generation and hold.get("user_end")
                and hold.get("until") and float(hold["until"]) <= now):
            return None
        _drop(doc, alias, {"lifted": now, "lifted_by": by, "ended": "the user's own end"}, now)
    return hold


def record_passed(alias: str, sig: str, at: float) -> bool:
    """Keep on the hold of `alias` when its end condition passed, as the project that checks it saw
    (pauseends), so every project with tasks on the machine reads it (passed_at). Only for the hold of
    that identity (`sig`, pauseends._sig): a re-set hold never inherits an old pass. Whether it wrote."""
    from . import pauseends
    want = {"sig": sig, "at": float(at)}
    hold = active().get(alias)
    if not hold or pauseends._sig(entry(hold)) != sig or hold.get(PASSED) == want:
        return False
    with _locked():
        doc = _read()
        hold = doc["holds"].get(alias)
        if not isinstance(hold, dict) or pauseends._sig(entry(hold)) != sig or hold.get(PASSED) == want:
            return False
        hold[PASSED] = want
        project.write_json(_path(FILE), doc, 0o600)
    return True


def passed_at(hold: dict, db=None) -> float | None:
    """When the end condition of the user's hold passed, if it did: kept on the hold (record_passed),
    else in `db`, the checking project's own record (pauseends.passed_at)."""
    from . import pauseends
    v, got = entry(hold), hold.get(PASSED)
    try:
        if isinstance(got, dict) and got.get("sig") == pauseends._sig(v):
            return float(got["at"])
    except (TypeError, ValueError):
        pass
    return pauseends.passed_at(db, f"{pauseends.FLEET_PREFIX}{hold.get('alias')}", v) if db is not None else None


def _drop(doc: dict, alias: str, how: dict, now: float) -> None:
    """Move the hold of `alias` to the lifted list (kept KEPT_S), under the caller's lock."""
    hold = doc["holds"].pop(alias)
    doc["lifted"] = [x for x in doc["lifted"] if now - float(x.get("lifted") or 0) < KEPT_S] + [{**hold, **how}]
    project.write_json(_path(FILE), doc, 0o600)


def covering(res: str, holds: dict[str, dict] | None = None) -> dict | None:
    """The active hold whose machine `res` is on, or None."""
    from . import machine_ledger as ml
    holds = active() if holds is None else holds
    for alias, hold in sorted(holds.items()):
        if ml.on_machine(alias)(res):
            return hold
    return None


def entry(hold: dict) -> dict:
    """A hold as one paused-resources entry."""
    return {**{k: hold[k] for k in END_FIELDS if k in hold}, "reason": hold.get("reason") or "",
            "since": hold.get("since"), "by": hold.get("by") or "user", "project": hold.get("project"),
            "generation": hold.get("generation"), "fleet": hold.get("id"), "machine": hold.get("alias")}


def names(cfg: dict, db=None) -> set[str]:
    """The resource names a project uses: its config `resources`, its shared ones, and the
    resource labels of its open tasks."""
    from . import shared
    from .coordinator import task_resources
    got = cfg.get("resources")
    out = {str(k) for k in got} if isinstance(got, dict) else set()
    out |= shared.names(cfg)
    try:
        for r in db.q("SELECT labels FROM tasks WHERE status NOT IN ('done','failed','cancelled')") if db else []:
            out |= task_resources({"labels": r["labels"]})
    except Exception:   # an unreadable task table still holds the configured names
        pass
    return out


def paused(cfg: dict, db=None, holds: dict[str, dict] | None = None) -> dict[str, dict]:
    """The project's resources the active holds cover (module doc), as paused-resources entries;
    each held machine's alias is always among them."""
    from . import machine_ledger as ml
    holds = active() if holds is None else holds
    if not holds:
        return {}
    used = names(cfg, db)
    out = {}
    for alias, hold in sorted(holds.items()):
        on = ml.on_machine(alias)
        for res in sorted({alias} | {r for r in used if on(r)}):
            out.setdefault(res, entry(hold))
    return out


def checker(hold: dict, here: str | None = None) -> str:
    """The project whose daemon checks the hold's end: its setter, or when the setter cannot act,
    the machine's recovery owner (then its fallback)."""
    from . import machine_ledger as ml
    setter = str(hold.get("project") or "")
    if setter and ml.can_act(setter, here)[0]:
        return setter
    for who in ml.chain(str(hold.get("alias") or "")):
        if who != setter and ml.can_act(who, here)[0]:
            return who
    return setter


def line(hold: dict, where=None, now: float | None = None) -> str:
    """One line for status, the digest and `ttp machines list`, with when a user hold's end condition
    passed (passed_at; `where` a Project also reads its own record): who holds it, its end, how to lift
    it, then why, so a line cut to the screen's width loses reason text, never the action."""
    from . import ends, pauseends
    who = "for the user" if hold.get("by") == "user" else f"by its {hold.get('by') or 'coordinator'}"
    v = entry(hold)
    db = getattr(where, "db", None)
    passed = passed_at(hold, db)
    end = pauseends.describe(v, where, passed, now)
    return (f"{hold.get('alias')} held machine-wide by {hold.get('project') or '?'} {who}" + (f"; {end}" if end else "")
            + f"; {LIFT}" + (f"; why: {hold['reason']}" if hold.get("reason") else "")
            + f" (hold {hold.get('id')} since {ends.shown(float(hold.get('since') or 0), where)}; every project's "
            f"tasks on it wait)")


def _passed(hold: dict, db, now: float) -> float | None:
    """When the expected end of a hold the user set passed, if it did: recorded where it is checked
    (passed_at), or its `until`. A `user_end` hold lifts at its end instead: None."""
    if hold.get("by") != "user" or hold.get("user_end"):
        return None
    at = passed_at(hold, db)
    if at is None and hold.get("until") and float(hold["until"]) <= now:
        at = float(hold["until"])
    return at


def waiting(db, alias: str) -> int:
    """How many of a project's queued tasks use a resource on machine `alias` (they wait on its hold)."""
    from . import machine_ledger as ml
    from .coordinator import task_resources
    on = ml.on_machine(alias)
    return sum(1 for t in db.q("SELECT labels FROM tasks WHERE status='queued'")
               if any(on(r) for r in task_resources({"labels": t["labels"]})))


def passed_alerts(db, where=None, now: float | None = None) -> list[tuple[str, str]]:
    """(alert key, text) for each hold the user set whose expected end passed while this project's
    tasks wait on it: a 'needs you' item (alerts.USER_BLOCKERS), cleared by alert_holds."""
    from . import ends
    now = time.time() if now is None else now
    out = []
    for alias, hold in sorted(active().items()):
        if (at := _passed(hold, db, now)) is None or not (n := waiting(db, alias)):
            continue
        out.append((f"{ALERT_KIND}:{hold.get('id')}:passed",
                    f"{alias} is still held for you since {ends.shown(float(hold.get('since') or 0), where)}; your "
                    f"expected end {ends.shown(at, where)} has passed; {n} of this project's tasks wait on it. "
                    f"{LIFT[0].upper()}{LIFT[1:]}."))
    return out


def alert_holds(db, arg: str, now: float) -> bool:
    """The alert `fleethold:<id>:passed` holds while that hold is in force with its end still passed
    (lifting or extending it ends it) and tasks here wait on it; never ended by age."""
    hid = arg.partition(":")[0]
    return any(str(h.get("id")) == hid and _passed(h, db, now) is not None and waiting(db, alias)
               for alias, h in active().items())
