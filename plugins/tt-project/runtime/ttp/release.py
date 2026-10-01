# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Newer tt-project releases. `ttp setup` installs a release into ~/.tt-project/lib/current; each
project's daemon compares that with its own harness runtime, says so while they differ and, with
`upgrade.auto` on, merges it into its own harness through `ttp upgrade` (one try per release)."""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import locks
from .project import HOME_DIR, Project

CHECK_S = 3600          # how often the daemon compares the installed release with its harness
HELD_RECHECK_S = 300    # a release held back by a push or an upgrade in flight is looked at again this soon
UPGRADE_TASK_TITLE = "Finish the tt-project template upgrade"
KV_RELEASE, KV_AUTO = "release", "upgrade_auto"


def installed() -> Path:
    return HOME_DIR / "lib" / "current"


def runtime_version(runtime: Path) -> str:
    try:
        m = re.search(r'__version__ = "([^"]+)"', (runtime / "ttp" / "__init__.py").read_text())
    except OSError:
        return "unknown"
    return m.group(1) if m else "unknown"


def runtime_commit(runtime: Path) -> str:
    try:
        return (runtime / "ttp" / "SOURCE_COMMIT").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _num(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def drift(p: Project) -> dict | None:
    """The installed release when it is newer than, or the same version from another commit as,
    the project's harness runtime; None when they match or nothing is installed."""
    lib = installed() / "runtime"
    if not (lib / "ttp" / "__init__.py").is_file():
        return None
    new_v, new_c = runtime_version(lib), runtime_commit(lib)
    cur_v, cur_c = runtime_version(p.harness / "runtime"), runtime_commit(p.harness / "runtime")
    if (new_v, new_c) == (cur_v, cur_c) or _num(new_v) < _num(cur_v):
        return None
    return {"installed": f"{new_v} ({new_c})", "current": f"{cur_v} ({cur_c})", "key": f"{new_v} {new_c}"}


def upgrade_lock(p: Project) -> Path:
    return p.state / "upgrade.lock"


def _taken(path: Path) -> bool:
    try:
        with open(path) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return False
            except OSError:
                return True
    except OSError:
        return False


def open_upgrade_task(p: Project) -> int | None:
    t = p.db.one("SELECT id FROM tasks WHERE kind='harness' AND title=? AND status NOT IN "
                 "('done','failed','cancelled') ORDER BY id LIMIT 1", (UPGRADE_TASK_TITLE,))
    return int(t["id"]) if t else None


def hold_reason(p: Project, d: dict) -> str:
    """Why the daemon must not start an automatic upgrade to `d` now ("" = go)."""
    if _taken(upgrade_lock(p)):
        return "an upgrade is in flight"
    pushes = [h for h in locks.held(p.state / "locks") if h.startswith("push:")]
    if pushes:
        return "a push is in flight"
    tid = open_upgrade_task(p)
    if tid:
        return f"harness task #{tid} finishes an earlier upgrade"
    if (p.db.kv(KV_AUTO) or {}).get("key") == d["key"]:
        return "already tried for this release"
    return ""


def launch(p: Project) -> None:
    """Run the installed release's `ttp upgrade --auto` for this project, detached: it restarts the
    daemon that started it (running workers are kept)."""
    p.logs.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with open(p.logs / "upgrade.log", "a") as log:
        log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} automatic upgrade\n")
        log.flush()
        subprocess.Popen([sys.executable, str(installed() / "bin" / "ttp"), "upgrade", p.name, "--auto",
                          "--project-dir", str(p.base)], cwd=str(p.base), env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def start(p: Project, d: dict) -> None:
    """Record the try first, so a crash or a conflict is never retried for the same release."""
    p.db.set_kv(KV_AUTO, {"key": d["key"], "from": d["current"], "to": d["installed"], "ts": time.time(),
                          "outcome": "running"})
    launch(p)


def finish(p: Project, outcome: str, **extra) -> None:
    rec = p.db.kv(KV_AUTO) or {}
    p.db.set_kv(KV_AUTO, {**rec, "outcome": outcome, "ended": time.time(), **extra})


def line(p: Project, db, cfg: dict) -> str:
    """One line while the installed release differs from the harness ("" otherwise)."""
    d = db.kv(KV_RELEASE)
    if not d:
        return ""
    text = f"tt-project {d['installed']} available, harness on {d['current']}"
    if not (cfg.get("upgrade") or {}).get("auto", True):
        return text + f" (upgrade.auto is off: `ttp upgrade {p.name}` applies it)"
    tid = open_upgrade_task(p)
    if tid:
        return text + f" (the merge needs harness task #{tid})"
    rec = db.kv(KV_AUTO) or {}
    if rec.get("key") == d["key"]:
        if rec.get("outcome") == "running":
            if _taken(upgrade_lock(p)) or time.time() - float(rec.get("ts") or 0) < 900:
                return text + " (upgrading now)"
            return text + " (the automatic upgrade did not finish: see logs/upgrade.log)"
        if rec.get("outcome") == "failed":
            return text + f" (the automatic upgrade failed: {rec.get('why') or 'see logs/upgrade.log'})"
    return text
