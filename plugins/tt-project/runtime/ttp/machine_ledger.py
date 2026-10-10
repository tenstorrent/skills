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
project, which the coordinator takes at high effort. When the owner cannot act (its daemon stopped or
stale, the project paused, its coordinator logged out or failing) for UNAVAILABLE_S, the condition goes to
the fallback, then to the user as an outage. A condition no co-tenant reported for CONDITION_GONE_S, or
reported cleared, is over. With no owner set nothing is routed: each co-tenant's digest names the gap once.

Change ledger. Every live change to a shared machine (a drop-in, a disabled recovery switch, a pause) is
recorded with `ttp machines change add <alias> "<what>" --undo "<cmd>" (--expires <t> | --until-probe
"<cmd>")`: who made it (the project), why (the text), how to undo it and when it ends. Each co-tenant's
digest lists the open entries for machines in its Resources, one line each. An entry is overdue when it
expires or its until-probe passes (exit 0: what it waited for is over, so the undo is due); a probe-only
entry also expires after PROBE_END_S. An overdue entry pages the project that made it, then the machine's
owner, then its fallback, each after the one before cannot act for UNAVAILABLE_S or let REPAGE_S pass,
then the user. Nothing is undone automatically: whoever is paged runs the undo and closes the entry
(`ttp machines change close <id>`). Probes run only in the daemon of the project that made the entry,
from its root, as end probes do.

Both files live next to machines.json, written whole under one lock, so co-tenant daemons and CLIs never
lose each other's writes. Other projects' state is only read (their database, read-only), never written.

A page is never lost nor sent twice: `route` records each due page as pending, with a text fixed for its
step, and hands it to one daemon at a time (a claim that lapses after CLAIM_S); that daemon marks it
`delivered` only once it went out, or `release`s it for the next tick. A crash in between leaves it
pending, and the resend is idempotent (same inbox text, same event fingerprint, same alert episode).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import ends, machines, project

LEDGER = "machine-changes.json"
ROUTES = "machine-routes.json"
LOCK = "machine-ledger.lock"
CLAIM_S = 120               # a pending page one daemon took and did not mark delivered is retried after this
UNAVAILABLE_S = 1800         # a target that cannot act this long is passed over
REPAGE_S = 86400             # an item still open this long after its page is paged again, one step on
GRACE_S = 1800               # default: how long a condition lasts before it is routed
CONDITION_GONE_S = 3 * 3600  # a condition nobody reported this long is over
PROBE_END_S = 7 * 86400      # an entry with only an until-probe is overdue after this anyway
CLOSED_KEPT_S = 30 * 86400   # closed entries kept this long
TEXT_CHARS = 300
CONDITIONS = ("down", "held")
GAP_KV = "machine_owner_gap_named"
USER = "the user"


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
               task: int | None = None, now: float | None = None) -> dict:
    """Record a live change to machine `alias` made by project `by`; returns the entry."""
    now = time.time() if now is None else now
    if alias not in machines.load():
        raise ValueError(f"no machine {alias!r} in `ttp machines list`")
    what, undo, probe = _one_line(what), _one_line(undo, 1000), _one_line(until_probe, ends.PROBE_CHARS)
    if not what or not undo:
        raise ValueError("say what changed and give the --undo command")
    if not expires and not probe:
        raise ValueError("give an end: --expires <delay or time> or --until-probe <command>")
    end = ends.parse_expires(expires, now) if expires else now + PROBE_END_S
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        n = int(doc.get("next") or 1)
        entry = {"id": n, "alias": alias, "what": what, "undo": undo, "by": by, "task": task, "added": now,
                 "expires": end, "probe": probe or None}
        doc["next"] = n + 1
        doc["changes"] = [c for c in doc.get("changes", []) if not c.get("closed")
                          or now - float(c["closed"]) < CLOSED_KEPT_S] + [entry]
        _write(LEDGER, doc)
    return entry


def changes(aliases=None, open_only: bool = True) -> list[dict]:
    doc = _read(LEDGER, {"changes": []})
    return [c for c in doc.get("changes", []) if isinstance(c, dict) and (not open_only or not c.get("closed"))
            and (aliases is None or c.get("alias") in aliases)]


def close_change(cid: int, why: str = "", by: str = "", now: float | None = None) -> dict:
    now = time.time() if now is None else now
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        for c in doc.get("changes", []):
            if c.get("id") == cid and not c.get("closed"):
                c.update(closed=now, closed_why=_one_line(why) or "closed", closed_by=by)
                _write(LEDGER, doc)
                return c
    raise ValueError(f"no open machine change #{cid}")


def overdue(c: dict, now: float | None = None) -> str:
    """Why entry `c` is overdue ("" when it is not)."""
    now = time.time() if now is None else now
    if c.get("closed"):
        return ""
    if c.get("probe_passed"):
        return "its until-probe passed"
    if float(c.get("expires") or 0) <= now:
        return f"expired {ends.stamp(float(c['expires']))}"
    return ""


def change_line(c: dict, now: float | None = None) -> str:
    late = overdue(c, now)
    end = f"until-probe `{c['probe']}`, at the latest {ends.stamp(c['expires'])}" if c.get("probe") \
        else f"ends {ends.stamp(c['expires'])}"
    by = c.get("by", "?") + (f" #{c['task']}" if c.get("task") else "")
    return (f"#{c['id']} {c['alias']}: {c['what']} (by {by}; undo: `{c['undo']}`; {end})"
            + (f" OVERDUE: {late}" if late else ""))


# conditions ---------------------------------------------------------------------------------------

def report(by: str, alias: str, condition: str, text: str = "", grace_s: float | None = None,
           cleared: bool = False, now: float | None = None) -> bool:
    """A co-tenant `by` sees `alias` `condition` (down or held), or no longer does (`cleared`).
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
        _write(ROUTES, doc)
    return True


def conditions() -> dict[str, dict]:
    return _read(ROUTES, {"conditions": {}}).get("conditions", {})


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
        status = overview._daemon(conn, state, here)
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


def _step(page: dict, targets: list[str], now: float, able: Callable[[str], tuple[bool, str]]) -> str | None:
    """Advance one item's page state; returns the target to page now, or None."""
    if "stage" not in page:
        page.update(stage=0, at=now)
        return targets[0] if targets else USER
    stage = int(page["stage"])
    if stage >= len(targets):
        return None   # the user has it
    ok, why = able(targets[stage])
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


def route(now: float | None = None, able: Callable[[str], tuple[bool, str]] | None = None,
          claimer: str = "") -> list[dict]:
    """The pages to deliver now, model-free: [{target, kind, key, alias, stage, id, text}]. Each due
    page is recorded as pending; the pending pages nobody holds (or whose claim lapsed after CLAIM_S)
    are claimed for `claimer` and returned. Mark each `delivered` once it went out, or `release` it."""
    now = time.time() if now is None else now
    able = able or can_act
    out: list[dict] = []

    def claim(page: dict) -> None:
        pend = page.get("pending")
        if pend and now - float(pend.get("claimed") or 0) >= CLAIM_S:
            pend.update(claimed=now, claimed_by=claimer)
            out.append({k: v for k, v in pend.items() if k not in ("claimed", "claimed_by")})

    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        conds = doc.setdefault("conditions", {})
        for key, c in list(conds.items()):
            if now - float(c.get("last") or 0) >= CONDITION_GONE_S:
                del conds[key]
                continue
            targets = chain(c["alias"])
            if not targets or now - float(c["first"]) < float(c.get("grace_s", GRACE_S)):
                continue
            page = c.setdefault("page", {})
            target = _step(page, targets, now, able)
            if target:
                skipped = f" ({targets[page['stage'] - 1]} cannot act: {page.get('why') or 'no answer'})" \
                    if page["stage"] else ""
                # No running count in the text: a resend after a crash is the same note.
                _pending(page, target, "machine_condition", f"machine:{key}", c["alias"], float(c["first"]),
                         f"Machine {c['alias']} {c['condition']} since {ends.stamp(c['first'])}, seen by "
                         f"{', '.join(c['seen_by'])}: {c.get('text') or 'no detail'}. "
                         f"Recovery page {page['stage'] + 1}{skipped}: "
                         f"check and recover it; record any live change with `ttp machines change add`.")
            claim(page)
        _write(ROUTES, doc)
        ledger = _read(LEDGER, {"next": 1, "changes": []})
        changed = False
        for c in ledger.get("changes", []):
            late = overdue(c, now)
            if not late:
                continue
            page = c.setdefault("page", {})
            target = _step(page, chain(c["alias"], c.get("by")), now, able)
            changed = True
            if target:
                _pending(page, target, "machine_change_overdue", f"machine-change:{c['id']}", c["alias"],
                         float(c["added"]),
                         f"Machine change #{c['id']} on {c['alias']} is overdue ({late}): {c['what']} "
                         f"(by {c.get('by')}). Page {page['stage'] + 1}: undo it with `{c['undo']}`, "
                         f"then `ttp machines change close {c['id']}`.")
            claim(page)
        if changed:
            _write(LEDGER, ledger)
    return out


def _settle(sent: dict, done: bool) -> bool:
    """Mark the pending page `sent` delivered (done) or free to retry; False when it is no longer pending."""
    with _locked():
        for name, empty in ((ROUTES, {"conditions": {}}), (LEDGER, {"next": 1, "changes": []})):
            doc = _read(name, empty)
            items = doc.get("conditions", {}).values() if name == ROUTES else doc.get("changes", [])
            for c in items:
                pend = (c.get("page") or {}).get("pending") if isinstance(c, dict) else None
                if not pend or pend.get("id") != sent.get("id"):
                    continue
                if done:
                    c["page"].pop("pending")
                    c["page"]["delivered"] = sent["id"]
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
    return isinstance(c, dict) and now - float(c.get("last") or 0) < CONDITION_GONE_S


def change_overdue(cid: int, now: float | None = None) -> bool:
    """Whether change #cid is open and overdue (alerts.holds)."""
    return any(c.get("id") == cid and overdue(c, now) for c in changes())


# probes -------------------------------------------------------------------------------------------

class Probes:
    """Runs the until-probes of the entries project `p` made, in the background, from its root."""

    def __init__(self, p, log: Callable[[str], None] = print):
        self.p, self.log = p, log
        self._procs: dict[int, tuple[subprocess.Popen, float]] = {}
        self._probed: dict[int, float] = {}

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        passed = []
        for cid, (proc, started) in list(self._procs.items()):
            rc = proc.poll()
            if rc is None and now - started < ends.PROBE_TIMEOUT_S:
                continue
            del self._procs[cid]
            if rc is None:
                ends._kill(proc)
            elif rc == 0:
                passed.append(cid)
        if passed:
            with _locked():
                doc = _read(LEDGER, {"next": 1, "changes": []})
                for c in doc.get("changes", []):
                    if c.get("id") in passed and not c.get("closed"):
                        c["probe_passed"] = now
                _write(LEDGER, doc)
        for c in changes():
            cid = c.get("id")
            if c.get("by") != self.p.name or not c.get("probe") or c.get("probe_passed") or cid in self._procs \
                    or now - self._probed.get(cid, 0) < ends.PROBE_EVERY_S:
                continue
            self._probed[cid] = now
            try:
                self._procs[cid] = (subprocess.Popen(c["probe"], shell=True, cwd=str(self.p.root),
                                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL, start_new_session=True), now)
            except OSError as e:
                self.log(f"machine change #{cid}: until-probe could not start: {e}")

    def stop(self) -> None:
        for proc, _ in self._procs.values():
            ends._kill(proc)
        self._procs.clear()


# digest -------------------------------------------------------------------------------------------

def resource_machines(p) -> list[str]:
    """The machines in the user's list that the charter's Resources section names."""
    from .prompts import charter_sections
    try:
        text = "\n".join("\n".join(body) for head, body in charter_sections(p.charter_path.read_text())
                         if ends.section_name(head).lower().startswith("resources"))
    except OSError:
        return []
    return sorted(a for a in machines.load() if re.search(rf"(?<![\w.-]){re.escape(a)}(?![\w-]|\.\w)", text))


def digest_lines(p, now: float | None = None) -> list[str]:
    """The open changes and conditions on the machines in `p`'s Resources, one line each, and each
    machine there without a recovery owner, named once per project."""
    now = time.time() if now is None else now
    mine = resource_machines(p)
    if not mine:
        return []
    known = machines.load()
    lines = [change_line(c, now) for c in changes(set(mine))]
    for c in conditions().values():
        if c.get("alias") in mine:
            owner = (known.get(c["alias"]) or {}).get("recovery_owner")
            lines.append(f"{c['alias']} {c['condition']} since {ends.stamp(c['first'])} (seen by "
                         f"{', '.join(c.get('seen_by', []))}; recovery owner: {owner or 'none set'})")
    named = list(p.db.kv(GAP_KV) or [])
    gaps = [a for a in mine if not (known.get(a) or {}).get("recovery_owner") and a not in named]
    if gaps:
        lines.append("No recovery owner set for " + ", ".join(gaps) + ": a long down or held condition there "
                     "reaches nobody. Set one: `ttp machines set <alias> --owner <project> [--fallback <project>]` "
                     "(named once).")
        p.db.set_kv(GAP_KV, named + gaps)
    return lines
