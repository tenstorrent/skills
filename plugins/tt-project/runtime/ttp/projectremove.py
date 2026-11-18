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

Every step acts on the project whose own folder was named, never another one (identity_refusal): the daemon,
service units and registry row are keyed by the project's configured name, so a folder whose harness/project.json
carries another project's name (a scratch project built from a copied config) would otherwise stop, uninstall and
unregister that other project. `ttp stop`, `ttp remove`, `--delete-files`, `ttp adopt` and the clean-up of a failed
`ttp new` refuse such a folder, and a live daemon, unit or registry row of that name that belongs elsewhere.

Files are deleted only with an explicit flag (`--delete-files`): the project's own tt-project folder (state, runs,
worktrees, harness) goes, the code repository beside it stays and its git worktree records are pruned. Without the
flag the folder stays where it is, so the project can be adopted again."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


def _res(path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path)


def unit_dir_of(text: str) -> str:
    """The WorkingDirectory= of a systemd unit's text ('' when it has none)."""
    return next((ln.split("=", 1)[1].strip() for ln in text.splitlines() if ln.startswith("WorkingDirectory=")), "")


def pid_dir(pid: int) -> Path | None:
    """The project folder a daemon process `pid` serves (its command line: `-m ttp.daemon <base>`), or None
    where it cannot be read (no /proc, another user's process) or the process is no tt-project daemon."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return None
    i = argv.index("ttp.daemon") if "ttp.daemon" in argv else -1
    return Path(argv[i + 1]) if 0 <= i < len(argv) - 1 and argv[i + 1] else None


def identity_refusal(p, name: str | None = None) -> str | None:
    """Why stopping, removing or adopting `p` could act on another project, else None. The service units, the
    registry row and the shared records go by p.name (its harness/project.json), so each must be p's own:
    - `name`, the name the command was given, is p's configured name;
    - the registry row of that name on this host points at p's folder;
    - an installed systemd unit or launchd agent of that name runs in p's folder;
    - the pid in p's daemon.pid, when it runs, runs in p's folder."""
    from . import project, service
    base = _res(p.base)
    if name and p.name != name:
        return (f"the folder {p.root} calls itself {p.name!r} in its harness/project.json, not {name!r}: its daemon, "
                f"service and registry row are those of {p.name!r}, so nothing was stopped or removed")
    entry = project.load_registry().get("projects", {}).get(p.name)
    if isinstance(entry, dict) and entry.get("dir") and entry.get("host") in (None, project.hostname()):
        if (other := _res(project.Project(entry["dir"]).base)) != base:
            return (f"the registered project {p.name!r} is at {other.parent}, not {p.root}: this folder carries "
                    f"another project's name, so nothing of {p.name!r} was stopped or removed")
    try:
        unit = service._unit(p)
        wd = unit_dir_of(unit.read_text()) if unit.exists() else ""
    except OSError:
        wd = ""
    if wd and _res(wd) != base:
        return f"the service {unit.name} runs the project at {wd}, not {p.base}: it is another project's, left alone"
    if sys.platform == "darwin":
        try:
            agent = service._agent(p)
            wd = plistlib.loads(agent.read_bytes()).get("WorkingDirectory", "") if agent.exists() else ""
        except (OSError, ValueError, AttributeError):
            wd = ""
        if wd and _res(wd) != base:
            return f"the launchd agent {agent.name} runs the project at {wd}, not {p.base}: it is another project's, left alone"
    from .daemon import _alive, _read_pid
    pid = _read_pid(Path(p.state) / "daemon.pid")
    if pid and pid != os.getpid() and _alive(pid) and (cwd := pid_dir(pid)) is not None and _res(cwd) != base:
        return (f"the pid {pid} in {p.state / 'daemon.pid'} is the daemon of {cwd}, not {p.base}: it is not this "
                "project's daemon, so it was not signalled")
    return None


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
    from . import project
    for name, entry in project.load_registry().get("projects", {}).items():
        if not (isinstance(entry, dict) and entry.get("dir") and entry.get("host") in (None, project.hostname())):
            continue
        other = _res(project.Project(entry["dir"]).base)
        if other != base and (base in other.parents or other == base.parent):
            return f"{base} holds another registered project's folder ({name} at {other})"
    return None


def delete_files(p) -> list[str]:
    """Delete `p`'s tt-project folder (and the real folders behind a macOS .noindex link), prune the code repo's
    git worktree records. The code repository itself stays."""
    why = delete_refusal(p)
    if why:
        return [f"files kept: {why}"]
    base = Path(p.base)
    gone = []
    for d in {Path(p.state), Path(p.worktrees)}:   # only the project's own moved folders, beside its links
        if d.name.endswith(".noindex") and d.is_dir() and not d.is_symlink() and _res(d).parent == _res(base):
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
    if why := identity_refusal(p):
        return [f"refused: {why}"]
    # A step that fails (its daemon or unit already gone, a record unreadable) is reported; the registry row
    # is dropped all the same, so a removed project never lingers as a registered one the deploy waits on.
    try:
        lines.append(f"stopped: {stop()}")
    except Exception as e:
        lines.append(f"not stopped: {str(e)[:160]}")
    lines.extend(prune_shared(p))
    try:
        entry = project.load_registry().get("projects", {}).get(p.name)
        if isinstance(entry, dict) and entry.get("dir") and _res(project.Project(entry["dir"]).base) != _res(p.base):
            lines.append(f"registry row {p.name} points elsewhere ({entry['dir']}): kept")
        else:
            (unregister or project.unregister)(p.name)
            lines.append(f"unregistered {p.name}")
    except Exception as e:
        lines.append(f"not unregistered {p.name}: {str(e)[:160]}")
    lines.extend(delete_files(p) if delete else [f"files kept in {p.base} (--delete-files removes them)"])
    return lines
