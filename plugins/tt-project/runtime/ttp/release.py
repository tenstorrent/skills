# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Newer tt-project releases. `ttp setup` installs a release into ~/.tt-project/lib/current; each
project's daemon compares that with its own harness runtime, says so while they differ and, with
`upgrade.auto` on, merges a strictly newer version into its own harness through `ttp upgrade` (one try
per release). The same version from another commit is only shown: a hand install or a rebuilt branch
must not trigger an unattended upgrade."""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import locks
from .project import HOME_DIR, Project, fsync_dir

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


def is_newer(a: str, b: str) -> bool:
    """Version `a` is strictly newer than `b` (an unknown version is older than any known one)."""
    return _num(a) > _num(b)


def older(p: Project) -> dict | None:
    """The installed release when it is an older version than the project's own harness runtime
    (an older runtime's setup replaced a newer install); None otherwise or when either is unknown."""
    lib = installed() / "runtime"
    if not (lib / "ttp" / "__init__.py").is_file():
        return None
    new_v, cur_v = runtime_version(lib), runtime_version(p.harness / "runtime")
    if not _num(new_v) or not _num(cur_v) or not is_newer(cur_v, new_v):
        return None
    out = {"installed": f"{new_v} ({runtime_commit(lib)})", "harness": cur_v}
    if forced_version() == new_v:
        out["forced"] = True
    return out


def forced_mark() -> Path:
    """Written by `ttp setup --force` when it installs an older version over a newer one; names
    that version. While lib/current still holds it, daemons leave the downgrade alone."""
    return HOME_DIR / "lib" / "forced-downgrade"


def forced_version() -> str:
    try:
        return forced_mark().read_text().strip()
    except OSError:
        return ""


def restorable(at_least: str) -> Path | None:
    """The newest complete release under ~/.tt-project/lib (runtime, template and launcher) whose
    version is `at_least` or newer; None when there is none."""
    best, best_v = None, ()
    for d in (HOME_DIR / "lib").iterdir() if (HOME_DIR / "lib").is_dir() else ():
        if d.name == "current" or d.is_symlink() or not d.is_dir():
            continue
        if not ((d / "runtime" / "ttp" / "__init__.py").is_file() and (d / "template").is_dir()
                and (d / "bin" / "ttp").is_file()):
            continue
        v = _num(runtime_version(d / "runtime"))
        if v and v >= _num(at_least) and v > best_v:
            best, best_v = d, v
    return best


def point_current(target: Path) -> None:
    """Re-point lib/current at `target` in one step (a new symlink renamed over the old one), so a
    `ttp` starting meanwhile sees the old release or the new one, never none."""
    cur = installed()
    tmp = cur.with_name(f".current.{os.getpid()}.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, cur)
    fsync_dir(cur.parent)


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
    return {"installed": f"{new_v} ({new_c})", "current": f"{cur_v} ({cur_c})", "key": f"{new_v} {new_c}",
            "newer": _num(new_v) > _num(cur_v)}


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


def push_in_flight(p: Project) -> bool:
    return any(h.startswith("push:") for h in locks.held(p.state / "locks"))


def hold_reason(p: Project, d: dict) -> str:
    """Why the daemon must not start an automatic upgrade to `d` now ("" = go)."""
    if _taken(upgrade_lock(p)):
        return "an upgrade is in flight"
    if push_in_flight(p):
        return "a push is in flight"
    tid = open_upgrade_task(p)
    if tid:
        return f"harness task #{tid} finishes an earlier upgrade"
    rec = p.db.kv(KV_AUTO) or {}
    if rec.get("key") == d["key"] and rec.get("outcome") != "held":
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
    if not d.get("newer", True):
        return text + " (same version from another commit: not applied automatically)"
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
