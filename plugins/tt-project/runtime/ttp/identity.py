# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""What code a running daemon loaded: its runtime identity receipt.

The daemon captures the receipt once, at startup, before its first tick: the version of the
imported package, the SOURCE_COMMIT next to it, the runtime path, pid, process start, boot id and a
random instance id. Nothing re-reads files later, and nothing infers the running version from git or
from the harness files: files on disk change under a running process, its imported code does not.
The receipt goes into the heartbeat and the start marker, so `ttp status`, the web app and
`ttp overview` can say what is really running, and when that differs from the files ("restart
pending"). A heartbeat without one (a daemon older than receipts) reads as unverified.

The time of the last completed tick is kept in its own file (TICK_FILE, its mtime), apart from the
heartbeat, which a long tick's steps also touch to tell the watchdogs it still moves.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

TICK_FILE = "tick_done"   # in state/: written once per daemon, its mtime is the last completed tick
SOURCE_FILE = "SOURCE_COMMIT"


def capture(mod=None, boot: str | None = None, started: float | None = None) -> dict:
    """The receipt of the code this process imported (`mod`: the ttp package). Provenance is
    'verified' only when SOURCE_COMMIT is there, the package's __init__.py on disk still names the
    imported version, and every loaded module of the package comes from its own directory."""
    if mod is None:
        import ttp as mod
    from .runner import boot_id, proc_start
    pkg = Path(mod.__file__).resolve().parent
    version = str(getattr(mod, "__version__", "") or "unknown")
    why = []
    try:
        commit = (pkg / SOURCE_FILE).read_text().strip()
    except OSError:
        commit = ""
    if not commit:
        why.append("no SOURCE_COMMIT")
    try:
        m = re.search(r'__version__ = "([^"]+)"', (pkg / "__init__.py").read_text())
        on_disk = m.group(1) if m else ""
    except OSError:
        on_disk = ""
    if on_disk != version:
        why.append(f"files say {on_disk or 'nothing'}, imported {version}")
    name = getattr(mod, "__name__", "ttp")
    stray = sorted(k for k, v in list(sys.modules.items())
                   if k.startswith(name + ".") and getattr(v, "__file__", None)
                   and Path(v.__file__).resolve().parent != pkg and pkg not in Path(v.__file__).resolve().parents)
    if stray:
        why.append("modules from elsewhere: " + ", ".join(stray[:3]))
    pid = os.getpid()
    return {"version": version, "commit": commit or None, "runtime": str(pkg.parent), "pid": pid,
            "proc_start": proc_start(pid), "started": started or time.time(), "boot": boot or boot_id(),
            "instance": secrets.token_hex(8), "provenance": "unverified" if why else "verified",
            **({"why": "; ".join(why)} if why else {})}


def mark_tick(state: Path, receipt: dict, first: bool) -> None:
    """A tick completed: the tick file's mtime moves to now. Written in full on this daemon's
    first tick (it names the instance, so another daemon's file is never read as this one's)."""
    f = state / TICK_FILE
    try:
        if first or not f.exists():
            from .project import durable_write
            durable_write(f, json.dumps({"instance": receipt.get("instance"), "pid": receipt.get("pid")}))
        else:
            os.utime(f, None)
    except OSError:
        pass


def tick_done(state: Path, receipt: dict | None) -> float | None:
    """When the daemon with this receipt last completed a full tick; None if unknown."""
    if not receipt or not receipt.get("instance"):
        return None
    f = state / TICK_FILE
    try:
        if json.loads(f.read_text()).get("instance") != receipt["instance"]:
            return None
        return f.stat().st_mtime
    except (OSError, ValueError, AttributeError):
        return None


def running(state: Path, pid: int) -> dict | None:
    """The receipt of daemon `pid` from its heartbeat, else from its start marker (a daemon still in
    its first tick); None when neither names that pid with a receipt (an older daemon)."""
    for name in ("heartbeat", "daemon.start"):
        try:
            d = json.loads((state / name).read_text())
        except (OSError, ValueError):
            continue
        r = d.get("receipt") if isinstance(d, dict) else None
        if isinstance(r, dict) and int(d.get("pid") or 0) == pid and int(r.get("pid") or 0) == pid:
            return r
    return None


HEARTBEAT_FRESH_S = 120   # a heartbeat this recent stands for a live daemon the pid probe cannot see


def probe(state: Path, rec: dict | None, here: str, now: float | None = None) -> dict:
    """Whether the project's daemon runs, as every CLI view tells it: {state: running, stale, unknown or
    stopped; alive; pid (0 unless known to run); source: pid or heartbeat; age of the heartbeat}.
    `rec` is the daemon's kv record {pid, host}. A sandboxed worker (TTP_RUN_DIR) may have its own pid
    namespace or hostname, so the pid probe can fail for a live daemon: a fresh heartbeat decides then."""
    from .daemon import HEARTBEAT_STALE_S, _alive
    now = time.time() if now is None else now
    rec = rec if isinstance(rec, dict) else {}
    pid = int(rec.get("pid") or 0)
    seen = bool(pid) and rec.get("host") == here and _alive(pid)
    try:
        hb = json.loads((state / "heartbeat").read_text())
        hb = hb if isinstance(hb, dict) else {}
        age = max(0.0, now - (state / "heartbeat").stat().st_mtime)
    except (OSError, ValueError):
        hb, age = None, None
    out = {"state": "stopped", "alive": False, "pid": 0, "source": None, "age": age}
    sandboxed = bool(os.environ.get("TTP_RUN_DIR"))
    if not seen or sandboxed:
        # the heartbeat stands in only for the daemon the record names (both carry its own pid)
        if hb is not None and age <= HEARTBEAT_FRESH_S and (seen or pid and int(hb.get("pid") or 0) == pid):
            return {**out, "state": "running", "alive": True, "pid": pid, "source": "pid" if seen else "heartbeat"}
        if not seen:
            if sandboxed and hb is not None and age <= HEARTBEAT_STALE_S:
                out["state"] = "unknown"
            return out
    if hb is not None and int(hb.get("pid") or 0) == pid and age > HEARTBEAT_STALE_S:
        return {**out, "state": "stale", "pid": pid, "source": "pid"}
    return {**out, "state": "running", "alive": True, "pid": pid, "source": "pid"}


def files_version(receipt: dict | None, fallback: Path) -> str:
    """The version a restart would load: the runtime the daemon was started from, else `fallback`."""
    from .release import runtime_version
    rt = Path(receipt["runtime"]) if receipt and receipt.get("runtime") else None
    return runtime_version(rt if rt and (rt / "ttp" / "__init__.py").is_file() else fallback)


def summary(state: Path, pid: int, fallback: Path, where=None, now: float | None = None) -> dict:
    """{running_version, running_commit, instance, started, tick_done, files_version, provenance,
    restart_pending, loaded_head, loaded_tree, current_tree, runtime_stale, line} for daemon `pid`
    (0: none running). `line` is the one line `ttp status` shows, times in the home zone (`where`).
    The loaded runtime tree is the one the daemon recorded at start; the current one is the harness
    checkout's (`fallback`) now. They differ: stale, and the daemon restarts itself on it."""
    from . import timefmt
    r = running(state, pid) if pid else None
    files = files_version(r, fallback)
    out = {"running_version": None, "running_commit": None, "instance": None, "started": None, "tick_done": None,
           "files_version": files, "provenance": None, "restart_pending": False, "loaded_head": None,
           "loaded_tree": None, "current_tree": None, "runtime_stale": False, "line": ""}
    if not pid:
        return out
    if not r:
        out.update(running_version="unverified", provenance="unverified", line="running version unverified")
        return out
    commit = (r.get("commit") or "")[:12]
    out.update(running_version=r.get("version"), running_commit=r.get("commit"), instance=r.get("instance"),
               started=r.get("started"), tick_done=tick_done(state, r), provenance=r.get("provenance"))
    since = timefmt.short(float(r["started"]), where, now) if r.get("started") else "?"
    line = f"running {r.get('version')}" + (f" ({commit})" if commit else "") + f" since {since}"
    if r.get("provenance") != "verified":
        line += f" (provenance unverified: {r.get('why') or 'unknown'})"
    if r.get("runtime_tree"):
        from .runtimeload import tree
        cur = tree(fallback)
        out.update(loaded_head=r.get("harness_head"), loaded_tree=r["runtime_tree"], current_tree=cur,
                   runtime_stale=bool(cur and cur != r["runtime_tree"]))
        line += f" · runtime tree {r['runtime_tree'][:10]}"
        if out["runtime_stale"]:
            line += f", checkout now {cur[:10]}: stale, the daemon restarts itself on it"
    if files != "unknown" and r.get("version") != files:
        out["restart_pending"] = True
        line = f"restart pending: running {r.get('version')}, files {files} · " + line
    out["line"] = line
    return out
