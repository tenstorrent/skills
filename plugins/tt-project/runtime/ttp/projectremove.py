# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp remove <project>`: take a project off this machine cleanly.

A scratch or finished project leaves traces beyond its own folder: a daemon, its service files, the registry row
and rows in the records every project on the machine shares. Removing it by hand misses some (a stale row in the
machine-wide worker record keeps counting against the others' limit), so this one code path does all of it:

1. stop the daemon and its workers (service.uninstall, the pid file, runner.stop_runs);
2. remove the service files (systemd or launchd units, its crontab lines);
3. prune the project's rows from the shared per-machine records (PRUNERS: the machine-wide worker record, the
   machine-wide alert claims it holds), never by hand-editing them;
4. unregister it (the registry row).

Files are deleted only with an explicit flag (`--delete-files`): the project's own tt-project folder (state, runs,
worktrees, harness) goes, the code repository beside it stays and its git worktree records are pruned. Without the
flag the folder stays where it is, so the project can be adopted again."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def pruners(p) -> list:
    """(what, function) for each shared per-machine record that carries rows of project `p`."""
    from . import alerts, machinecap
    return [("machine-wide worker record", lambda: machinecap.forget(p.name)),
            ("machine-wide alert claims", lambda: bool(alerts.forget_claims(str(p.base))))]


def prune_shared(p) -> list[str]:
    """Prune every shared record of `p`'s rows; the lines say which held one (an unreadable record is reported, never fatal)."""
    out = []
    for what, fn in pruners(p):
        try:
            out.append(f"pruned {what}" if fn() else f"no row in {what}")
        except Exception as e:
            out.append(f"not pruned {what}: {str(e)[:120]}")
    return out


def delete_refusal(p) -> str | None:
    """Why `p`'s folder may not be deleted, else None: it must be a tt-project folder holding a project, and not the
    home or root of the file system or a parent of the code repo itself."""
    base = Path(p.base).resolve()
    if base.name != "tt-project" or not (base / "harness").is_dir():
        return f"{base} is not a tt-project folder with a harness"
    if base in (Path("/"), Path.home().resolve()) or base.parent == Path("/"):
        return f"{base} is too broad to delete"
    return None


def delete_files(p) -> list[str]:
    """Delete `p`'s tt-project folder (and the real folders behind a macOS .noindex link), prune the code repo's
    git worktree records. The code repository itself stays."""
    why = delete_refusal(p)
    if why:
        return [f"files kept: {why}"]
    base = Path(p.base)
    gone = []
    for d in {Path(p.state), Path(p.worktrees)}:
        if d.name.endswith(".noindex") and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(base, ignore_errors=True)
    gone.append(f"deleted {base}")
    try:
        subprocess.run(["git", "-C", str(p.root), "worktree", "prune"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass
    if base.exists():
        gone.append(f"files left in {base} (not all could be removed)")
    return gone


def remove(p, stop, delete: bool = False, unregister=None) -> list[str]:
    """Do the removal; `stop` is a callable that stops the daemon and its workers (the CLI's service stop) and
    returns its report. Returns one line per step."""
    from . import project
    lines = []
    lines.append(f"stopped: {stop()}")
    lines.extend(prune_shared(p))
    (unregister or project.unregister)(p.name)
    lines.append(f"unregistered {p.name}")
    lines.extend(delete_files(p) if delete else [f"files kept in {p.base} (--delete-files removes them)"])
    return lines
