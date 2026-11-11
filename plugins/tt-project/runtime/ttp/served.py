# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Account-wide served-model watch: one model-free /models poll per host and chat endpoint, shared by
every project on the host, so a model that goes away (or a new one) is known within a minute everywhere.

The served list lives under project.HOME_DIR/served/<endpoint id>.json, the endpoint id being the first
12 hex digits of the SHA-256 of its base URL: the ids, the check time, ok, its HTTP status line, the poll
interval in force and, per id ever listed, first_seen (the start of its current stretch of being listed),
last_seen and gone (when a poll first left it out). Each id that appears or goes away is one line in
served/served-events.jsonl. Neither the key nor the URL is ever written.

Each project daemon with a chat endpoint (providers.chat.endpoint) asks every few seconds whether a poll
is due (due: no network, no secrets). It is due once the shared list is EVERY_S old while any project on
the host routes work to chat (a tier, a routes rule, a run class, babysitting or a ranked model pin:
route.routes_chat; each such daemon touches the endpoint's busy marker), else IDLE_S old; both are
account keys (providers.chat.served_every_s, providers.chat.served_idle_s). Of the daemons that find it
due, the one that takes the poll lease (an flock-guarded record, renewed by each poll, TTL LEASE_S) polls
with its own saved key; the others skip until the lease lapses, so a holder that dies is replaced after
LEASE_S. The poller also writes its own project's per-project list (route.MODELS_FILE), the fallback.

route.read_served reads the shared list of the project's endpoint first (the newer of it and the project
file); a list over stale_s (STALE_POLLS polls) old counts as unknown (modelrank.served): no swap rests on it.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

DIR = "served"                    # under project.HOME_DIR
EVENTS = "served-events.jsonl"    # under DIR: {"at", "endpoint", "id", "event": "appeared" | "gone"}
LINK = "served-link.json"         # under a project's state dir: {provider: endpoint id} of its endpoint
EVERY_S = 30                      # the poll interval while any project on the host routes to chat
IDLE_S = 300                      # else
LEASE_S = 90                      # a poll lease nobody renewed for this long is free
STALE_POLLS = 3                   # a list older than this many poll intervals is unknown
KEEP_GONE_S = 30 * 86400          # a gone id's seen record is kept this long
EVENTS_MAX = 1_000_000            # past this size the events file keeps its newer half
CHECK_S = 5                       # the daemon asks due() at most this often


def endpoint_id(url: str) -> str:
    return hashlib.sha256(str(url or "").rstrip("/").encode()).hexdigest()[:12]


def home() -> Path:
    from .project import HOME_DIR
    return HOME_DIR / DIR


def _int(dotted: str, default: int) -> int:
    from .project import account_value
    v = account_value(dotted)
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else default


def every_s(busy: bool) -> int:
    """The poll interval: providers.chat.served_every_s (EVERY_S) while chat routes, else served_idle_s (IDLE_S)."""
    from .route import TO
    return _int(f"providers.{TO}.served_every_s", EVERY_S) if busy else _int(f"providers.{TO}.served_idle_s", IDLE_S)


def stale_s(rec: dict) -> float:
    """How old a served list may be before it counts as unknown: STALE_POLLS of its own poll interval."""
    v = rec.get("every") if isinstance(rec, dict) else None
    return STALE_POLLS * (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else EVERY_S)


def _json(path: Path) -> dict:
    try:
        v = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def read(eid: str) -> dict:
    """The shared served list of endpoint `eid`, or {}."""
    return _json(home() / f"{eid}.json") if eid else {}


def link(state: Path, prov: str) -> str:
    """The endpoint id the project's daemon last polled or read for `prov`, or ''."""
    v = _json(Path(state) / LINK).get(prov)
    return v if isinstance(v, str) else ""


def _set_link(state: Path, prov: str, eid: str) -> None:
    data = _json(Path(state) / LINK)
    if data.get(prov) != eid:
        from .project import durable_write
        durable_write(Path(state) / LINK, json.dumps({**data, prov: eid}))


def _busy_file(eid: str) -> Path:
    return home() / f"{eid}.busy"


def want(eid: str, now: float) -> None:
    """A project on the host routes work to endpoint `eid`: keep its poll at the busy interval."""
    path = _busy_file(eid)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    os.utime(path, (now, now))


def busy(eid: str, now: float) -> bool:
    """Some project on the host asked for `eid`'s busy interval within LEASE_S."""
    try:
        return now - _busy_file(eid).stat().st_mtime < LEASE_S
    except OSError:
        return False


def due(state: Path, prov: str, routes: bool, now: float) -> bool:
    """Whether the project's daemon should try a poll (no network, no secrets): its endpoint is unknown yet
    or its shared list is past the interval in force. Touches the busy marker when `routes`."""
    eid = link(state, prov)
    if not eid:
        return True
    if routes:
        want(eid, now)
    rec = read(eid)
    return now - float(rec.get("checked") or 0) >= every_s(busy(eid, now))


def take(eid: str, holder: str, now: float) -> bool:
    """Take or renew `eid`'s poll lease for `holder` until now + LEASE_S: False while another holder's
    lease stands."""
    path = home() / f"{eid}.lease"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(home() / f"{eid}.lock", "a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        cur = _json(path)
        if cur.get("holder") not in (None, holder) and float(cur.get("until") or 0) > now:
            return False
        from .project import durable_write
        durable_write(path, json.dumps({"holder": holder, "pid": os.getpid(), "until": now + LEASE_S, "at": now}))
    return True


def record(eid: str, ok: bool, check: str, ids: list[str] | None, every: int, now: float) -> list[dict]:
    """Write one poll's answer to the shared list; the events it appended (ids that appeared or went)."""
    from .project import durable_append, durable_write
    old = read(eid)
    seen: dict[str, Any] = {k: dict(v) for k, v in (old.get("seen") or {}).items() if isinstance(v, dict)}
    events: list[dict] = []
    if ok:
        listed = sorted(set(ids or []))
        for i in listed:
            s = seen.get(i) or {}
            if not s or s.get("gone"):
                s = {"first_seen": now}
                events.append({"at": now, "endpoint": eid, "id": i, "event": "appeared"})
            seen[i] = {**s, "last_seen": now}
        for i, s in seen.items():
            if i not in listed and not s.get("gone"):
                s["gone"] = now
                events.append({"at": now, "endpoint": eid, "id": i, "event": "gone"})
        seen = {i: s for i, s in seen.items() if not s.get("gone") or now - float(s["gone"]) < KEEP_GONE_S}
    else:
        listed = list(old.get("ids") or [])
    data = {"ids": listed, "checked": now, "ok": bool(ok), "check": check, "every": every, "seen": seen,
            **({"listed": old.get("checked")} if not ok and old.get("checked") else {})}
    durable_write(home() / f"{eid}.json", json.dumps(data, indent=1))
    if events:
        path = home() / EVENTS
        durable_append(path, "".join(json.dumps(e) + "\n" for e in events))
        if path.stat().st_size > EVENTS_MAX:
            lines = path.read_text().splitlines(keepends=True)
            durable_write(path, "".join(lines[len(lines) // 2:]))
    return events


def entry(rec: dict) -> dict:
    """A shared list as route.read_served's per-provider record."""
    keep = ("ids", "checked", "ok", "check", "every", "listed")
    return {k: rec[k] for k in keep if k in rec}


def tick(state: Path, alias: str, routes: bool, now: float | None = None) -> str:
    """One try at the poll for the project at `state` with the key saved for `alias`: 'polled', 'not due',
    'held' (another daemon's lease stands) or 'no key'. Raises OSError when the shared files cannot be
    written (the caller falls back to the per-project check)."""
    from . import endpoint, route
    now = time.time() if now is None else now
    got = endpoint.credentials(alias)
    if got is None:
        return "no key"
    eid = endpoint_id(got.url)
    _set_link(state, route.TO, eid)
    if routes:
        want(eid, now)
    every = every_s(busy(eid, now))
    if now - float(read(eid).get("checked") or 0) < every:
        return "not due"
    if not take(eid, str(Path(state).resolve()), now):
        return "held"
    ok, msg, ids = endpoint.list_models(got.url, got.key)
    record(eid, ok, msg, ids, every, now)
    route.write_served(state, route.TO, ok, msg, ids, now, every=every)
    return "polled"
