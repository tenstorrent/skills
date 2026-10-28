# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A device broker's pending power cycle of this host: host-wide maintenance holds while one is announced.

A device box can also host projects, and its device broker power-cycles the host after a chip or tray
fault. The open-source broker (tt-device-mcp) announces the cycle in a notice file, by default
NOTICE (`/run/tt-device-broker/power-cycle-pending.json`): absent, no cycle is pending; present, a JSON
object with `state` ("possible" or "requested"), `fault_class`, `chips`, `reason`, `hold_since`,
`earliest_cycle_at` and `updated_at`. It comes about 90 s before the cycle request and about 3 min before
power-off (a whole-tray drop gives only seconds), and the cycle never waits for anyone.

While a notice holds, nothing host-wide starts here: `ttp upgrade` and `ttp setup` exit 75 (busy) with
one line naming the state, reason and earliest cycle time, the daemon starts no automatic upgrade or
host-side takeover, and a post-push deploy (delivery.after_push) waits. Each retries by itself once the
file is gone, spending no attempt and counting no failure.

The paths come from the project's `upgrade.cycle_notices` (a list; [] turns the hold off), else NOTICE.
`ttp setup`, which belongs to no project, reads NOTICE plus every local project's list. A path that does
not exist holds nothing, so a host without a broker is unaffected. A notice holds only in a hold state.
A file that is present but unreadable or not such an object holds only MALFORMED_FRESH_S after its last
write, and a well-formed one only VALID_FRESH_S after its `updated_at` (or its last write, if later): a
stale file left by a broker that died never holds maintenance forever. `ttp upgrade --ignore-cycle-notice`
(and `ttp setup --ignore-cycle-notice`) proceeds anyway, with a warning.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

NOTICE = "/run/tt-device-broker/power-cycle-pending.json"
CONFIG_KEY = "upgrade.cycle_notices"
HOLD_STATES = ("possible", "requested")
MALFORMED_FRESH_S = 600      # a half-written or garbled notice: a broker rewrites it within seconds
VALID_FRESH_S = 6 * 3600     # a notice no broker refreshed in this long is a leftover
BUSY = 75                    # the exit code of a held `ttp upgrade` or `ttp setup` (as `ttp lock`'s busy)
TEST_ENV = "TTP_TEST_CYCLE_NOTICES"   # tests: the default paths (os.pathsep-separated), never the real one


def default_paths() -> list[str]:
    if TEST_ENV in os.environ:
        return [x for x in os.environ[TEST_ENV].split(os.pathsep) if x]
    return [NOTICE]


def paths(cfg: dict | None) -> list[str]:
    """The notice paths of a project's config: upgrade.cycle_notices, else the default."""
    v = ((cfg or {}).get("upgrade") or {}).get("cycle_notices") if isinstance(cfg, dict) else None
    if v is None:
        return default_paths()
    if isinstance(v, str):
        v = [v]
    return [str(x) for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else default_paths()


def project_paths(p) -> list[str]:
    try:
        return paths(p.config())
    except Exception:       # an unreadable config still holds on the default notice
        return default_paths()


def host_paths() -> list[str]:
    """The default paths plus every local project's upgrade.cycle_notices (for `ttp setup`)."""
    from .project import Project, hostname, load_registry
    out = list(default_paths())
    try:
        here = hostname()
        for e in (load_registry().get("projects") or {}).values():
            if isinstance(e, dict) and e.get("host") in (None, here) and e.get("dir"):
                q = Project(e["dir"])
                if q.exists():
                    out += [x for x in project_paths(q) if x not in out]
    except Exception:
        pass
    return out


def _epoch(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def read(path: str, now: float | None = None) -> dict | None:
    """The hold one notice file makes now, or None: {"path", "state", "reason", "earliest", "malformed"}."""
    now = time.time() if now is None else now
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None          # absent (or not reachable): no cycle pending
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("state"), str):
        if now - mtime >= MALFORMED_FRESH_S:
            return None
        return {"path": path, "state": "unreadable", "reason": "the notice is present but unreadable or malformed",
                "earliest": None, "malformed": True}
    state = data["state"].strip().lower()
    if state not in HOLD_STATES:
        return None
    seen = max(mtime, _epoch(data.get("updated_at")) or 0.0)
    if now - seen >= VALID_FRESH_S:
        return None
    reason = str(data.get("reason") or data.get("fault_class") or "no reason given")[:200]
    return {"path": path, "state": state, "reason": reason, "earliest": data.get("earliest_cycle_at"),
            "malformed": False}


def held(paths_: list[str], now: float | None = None) -> dict | None:
    """The first notice among `paths_` that holds, or None."""
    for path in paths_:
        h = read(path, now)
        if h:
            return h
    return None


def line(h: dict, where=None) -> str:
    """One line saying why maintenance waits."""
    e = h.get("earliest")
    ts = _epoch(e)
    if ts is not None:
        from . import timefmt
        try:
            at = timefmt.long(ts, where)
        except Exception:
            at = str(e)
    else:
        at = str(e) if e else "not given"
    return (f"a power cycle of this host is pending (state {h['state']}, reason: {h['reason']}, "
            f"earliest cycle at {at}; notice {h['path']})")
