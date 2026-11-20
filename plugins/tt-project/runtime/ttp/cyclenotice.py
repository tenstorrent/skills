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
stale file left by a broker that died never holds maintenance forever.

Probe announcements. A broker's health probe or status command may say a cycle is coming before (or
without) a notice file, e.g. 'next: power cycle ...'. The project's heal checks (heal.py) keep their last
output; project_held also holds while one that watches this host (no `machine` or `host`, or one naming
this host) printed a line matching `upgrade.cycle_patterns` (regexes, case-insensitive; default
DEFAULT_PATTERNS, [] turns it off) within PROBE_FRESH_S of its last run. `upgrade.cycle_checks` names
the checks to read (default: every local one). A default match needs a time or countdown in the line and
no 'no', 'none', 'disabled' and the like beside it. It clears as soon as the check's next output no longer
matches, or once the same line has held PROBE_MAX_HOLD_S (told once as a low event). While either source holds, besides the maintenance above, the push queue starts no new batch
(its checks would be cut), `ttp checks` starts none and `ttp detach` starts no local job (each exits 75). `ttp upgrade --ignore-cycle-notice`
(and `ttp setup --ignore-cycle-notice`) proceeds anyway, with a warning.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

NOTICE = "/run/tt-device-broker/power-cycle-pending.json"
CONFIG_KEY = "upgrade.cycle_notices"
HOLD_STATES = ("possible", "requested")
MALFORMED_FRESH_S = 600      # a half-written or garbled notice: a broker rewrites it within seconds
VALID_FRESH_S = 6 * 3600     # a notice no broker refreshed in this long is a leftover
BUSY = 75                    # the exit code of a held `ttp upgrade` or `ttp setup` (as `ttp lock`'s busy)
# A pending cycle in the generic words a probe uses; site-specific wording goes in upgrade.cycle_patterns.
DEFAULT_PATTERNS = (r"\bnext:\s*power[ -]?cycle\b",
                    r"\bpower[ -]?cycle\b[^\n]{0,40}\b(?:pending|requested|scheduled|imminent)\b")
PROBE_FRESH_S = 900          # a probe output older than this says nothing about now
# A default match is no announcement when a 'no' or a setting sits beside it, or the line gives no time or
# countdown ('auto power cycle: scheduled on chip loss' is a setting); configured patterns are taken as given.
_NEGATED = re.compile(r"\b(?:no|none|not|never|false|disabled|off)\b|[:=]\s*0\b", re.I)
_WHEN = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d+(?:\.\d+)?\s*(?:s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\b", re.I)
PROBE_MAX_HOLD_S = 1800      # no real cycle is announced this long ahead: the same line past it is stuck
SEEN_KEY = "cyclenotice_probe_seen"   # {line fingerprint: when it first held}
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
            f"earliest cycle at {at}; {'' if h['path'].startswith('heal check ') else 'notice '}{h['path']})")


def patterns(cfg: dict | None) -> list[re.Pattern]:
    """upgrade.cycle_patterns compiled (an invalid one is skipped), else DEFAULT_PATTERNS."""
    v = ((cfg or {}).get("upgrade") or {}).get("cycle_patterns") if isinstance(cfg, dict) else None
    if isinstance(v, str):
        v = [v]
    out = []
    for x in v if isinstance(v, list) else DEFAULT_PATTERNS:
        try:
            out.append(re.compile(str(x), re.I))
        except re.error:
            continue
    return out


def _announces(ln: str, pats: list[re.Pattern], strict: bool) -> bool:
    """Whether line `ln` announces a cycle: a pattern matches it (with `strict`, the defaults: no negation
    or setting beside the match, and a time or countdown in the line)."""
    for x in pats:
        m = x.search(ln)
        if m and (not strict or _WHEN.search(ln)
                  and not _NEGATED.search(ln[max(0, m.start() - 16):m.start()] + " " + ln[m.end():m.end() + 24])):
            return True
    return False


def _local(spec: dict) -> bool:
    """Whether a heal check watches this host: it names no other machine (`machine`, a broker or outage
    check's `resource`: heal.machine_of) or ssh host."""
    from . import heal, machines
    from .project import hostname
    names = [str(spec.get(k)).strip().lower() for k in ("machine", "host") if spec.get(k)]
    try:
        if m := heal.machine_of(spec):
            names.append(str(m).strip().lower())
    except Exception:
        pass
    if not names:
        return True
    me = {hostname().lower()}
    try:
        if h := machines.here():
            me |= {h[0].lower(), str(h[1].get("hostname") or "").lower()}
    except Exception:
        pass
    return all(n in me for n in names)


def probe_held(p, cfg: dict | None = None, now: float | None = None) -> dict | None:
    """A hold (as read() returns) from a heal check whose fresh last output announces a cycle, or None.
    The same line (by check and text) holds at most PROBE_MAX_HOLD_S: past it, it is released, told once
    as a low event, and holds again only after the output changed."""
    import hashlib
    from . import heal
    now = time.time() if now is None else now
    cfg = p.config() if cfg is None else cfg
    pats = patterns(cfg)
    names = ((cfg or {}).get("upgrade") or {}).get("cycle_checks")
    if not pats:
        return None
    strict = not isinstance(((cfg or {}).get("upgrade") or {}).get("cycle_patterns"), (list, str))
    seen = p.db.kv(SEEN_KEY) or {}
    seen = seen if isinstance(seen, dict) else {}
    keep, hold = {}, None
    for r, spec in heal.checks(p.db):
        if isinstance(names, list) and r["name"] not in names or names is None and not _local(spec):
            continue
        st = heal.state(p.db, r["name"])
        if now - float(st.get("last_check") or 0) >= PROBE_FRESH_S:
            continue
        for ln in str(st.get("last_out") or "").splitlines():
            if not _announces(ln, pats, strict):
                continue
            text = " ".join(ln.split())[:200]
            fp = hashlib.sha1(f"{r['name']}\n{text}".encode()).hexdigest()[:16]
            was = seen.get(fp) if isinstance(seen.get(fp), dict) else {}
            keep[fp] = {"first": float(was.get("first") or now), "told": bool(was.get("told"))}
            if now - keep[fp]["first"] > PROBE_MAX_HOLD_S:
                if not keep[fp]["told"]:
                    keep[fp]["told"] = True
                    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                           (now, "daemon", "observation", f"cycle-probe-stuck:{fp}", "low",
                            f"heal check {r['name']} has announced a power cycle for over "
                            f"{PROBE_MAX_HOLD_S // 60} min with the same line, so it no longer holds work: {text}",
                            "queued"))
                continue
            hold = hold or {"path": f"heal check {r['name']}", "state": "announced", "reason": text,
                            "earliest": None, "malformed": False}
    if keep != seen:
        p.db.set_kv(SEEN_KEY, keep)
    return hold


def project_held(p, cfg: dict | None = None, now: float | None = None) -> dict | None:
    """A notice file (held) or a probe announcement (probe_held) of a cycle of this host, or None."""
    try:
        cfg = p.config() if cfg is None else cfg
    except Exception:
        cfg = {}
    if h := held(paths(cfg) if cfg else project_paths(p), now):
        return h
    try:
        return probe_held(p, cfg, now)
    except Exception:       # an unreadable heal state never holds work
        return None
