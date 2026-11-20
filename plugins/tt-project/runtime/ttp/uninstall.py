# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp uninstall`: remove what ttp installed for this user on this machine.

It stops and removes every tt-project service in the user's service folders (project daemons, their
watchdogs, the notifier, kept web tunnels: systemd user units named tt-project-* or com.tt-project.*,
launchd agents com.tt-project.*), the crontab lines service.py wrote (each recognised whole: it ends
with that project's `# tt-project:<folder>` tag and runs `-m ttp.daemon <folder>`), any project daemon
still running, the /tt-project shortcut, the `ttp` shim setup wrote, the installed lib and the PATH line
install.sh added to a shell profile (only lines ending with its marker). It never
deletes project folders or their tt-project/ state, never changes linger and never touches another
crontab line. What it leaves is printed with the commands that remove it by hand.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import hoststate, project, shortcut
from .daemon import _is_daemon, _read_pid
from .providers.claude import claude_config_dir
from .service import CRON_TAG

MARKETPLACE = "tenstorrent-skills"
UNIT_RE = re.compile(r"^(tt-project-.+|com\.tt-project\..+)\.(service|timer)$")
AGENT_RE = re.compile(r"^com\.tt-project\..+\.plist$")

PATH_MARK = "# added by tt-project installer"      # install.sh ends its PATH line with this
Action = tuple[str, Callable[[], str]]   # what it does, and the step (returns a problem, or "")


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(hoststate.argv(argv), capture_output=True, text=True, timeout=60)


def _unit_dir() -> Path:
    return hoststate.unit_dir()   # a test run's fake under its marker


def _agent_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def local_projects() -> dict[str, Path]:
    """This machine's registered projects: name -> project root."""
    host = project.hostname()
    return {n: Path(e["dir"]) for n, e in sorted(project.load_registry().get("projects", {}).items())
            if isinstance(e, dict) and e.get("dir") and e.get("host", host) == host}


def _crontab() -> str | None:
    if not shutil.which("crontab"):
        return None
    r = _run("crontab", "-l")
    return r.stdout if r.returncode == 0 else ""


def cron_line_base(line: str) -> str | None:
    """The project folder of a crontab line service.py wrote, else None (any other line is the user's)."""
    m = re.search(re.escape(f" {CRON_TAG}") + r"(/.+)$", line.rstrip("\n"))
    if not m:
        return None
    base = m.group(1)
    return base if f"-m ttp.daemon {shlex.quote(base)} " in line else None


def _units() -> list[Path]:
    d = _unit_dir()
    found = sorted(f for f in d.iterdir() if UNIT_RE.match(f.name) and (f.is_file() or f.is_symlink())) \
        if d.is_dir() else []
    return sorted(found, key=lambda f: f.suffix != ".timer")     # timers first: they would start their service


def _agents() -> list[Path]:
    d = _agent_dir()
    return sorted(f for f in d.iterdir() if AGENT_RE.match(f.name)) if d.is_dir() else []


def _stop_unit(f: Path) -> Callable[[], str]:
    def step() -> str:
        err = ""
        if shutil.which("systemctl"):
            r = _run("systemctl", "--user", "disable", "--now", f.name)
            err = "" if r.returncode == 0 else f"systemctl: {r.stderr.strip()[:200]}"
        f.unlink(missing_ok=True)
        return err
    return step


def _stop_agent(f: Path) -> Callable[[], str]:
    def step() -> str:
        if shutil.which("launchctl"):
            _run("launchctl", "bootout", f"gui/{os.getuid()}/{f.stem}")   # not loaded: nothing to stop
        f.unlink(missing_ok=True)
        return ""
    return step


def _drop_cron_lines() -> str:
    cur = _crontab()
    if not cur:
        return ""
    lines = cur.splitlines(keepends=True)
    keep = [ln for ln in lines if cron_line_base(ln) is None]
    if len(keep) == len(lines):
        return ""
    r = subprocess.run(hoststate.argv(["crontab", "-"]), input="".join(keep), text=True, capture_output=True, timeout=60)
    return "" if r.returncode == 0 else f"crontab: {r.stderr.strip()[:200]}"


def _end_daemon(pidfile: Path) -> Callable[[], str]:
    def step() -> str:
        pid = _read_pid(pidfile)
        if pid and _is_daemon(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as e:
                return f"kill {pid}: {e}"
        return ""
    return step


def is_shim(f: Path) -> bool:
    try:
        return str(project.HOME_DIR / "lib") in f.read_text(errors="replace")
    except OSError:
        return False


def _profiles() -> list[Path]:
    """Every shell profile install.sh may add its PATH line to."""
    home = Path.home()
    zdot = Path(os.environ.get("ZDOTDIR") or home)
    files = [home / ".bashrc", home / ".bash_profile", home / ".bash_login", home / ".zshrc", zdot / ".zshrc",
             home / ".profile", home / ".config" / "fish" / "conf.d" / "tt-project.fish",
             Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "fish" / "conf.d" / "tt-project.fish"]
    return list(dict.fromkeys(files))


def _marked(f: Path) -> list[str]:
    try:
        return [ln for ln in f.read_text(errors="replace").splitlines() if ln.rstrip().endswith(PATH_MARK)]
    except OSError:
        return []


def _drop_path_line(f: Path) -> Callable[[], str]:
    """Remove only the marked lines, keeping every other byte; the fish file is the installer's own, so
    it goes once nothing else is left in it."""
    def step() -> str:
        try:
            mark = PATH_MARK.encode()
            keep = b"".join(ln for ln in f.read_bytes().splitlines(keepends=True) if not ln.rstrip().endswith(mark))
            if not keep.strip() and f.name == "tt-project.fish":
                f.unlink()
            else:
                project.durable_write(f.resolve(), keep)  # resolve: a symlinked dotfile stays a symlink
        except OSError as e:
            return f"{f}: {e}"
        return ""
    return step


def plan(bin_dir: Path) -> list[Action]:
    """Every step a real run takes, in order: services first, so none restarts what the later steps end."""
    acts: list[Action] = [(f"stop and remove systemd user unit {f.name}", _stop_unit(f)) for f in _units()]
    if acts and shutil.which("systemctl"):
        acts.append(("reload systemd user units", lambda: _run("systemctl", "--user", "daemon-reload") and ""))
    acts += [(f"stop and remove launchd agent {f.stem}", _stop_agent(f)) for f in _agents()]
    mine = [ln for ln in (_crontab() or "").splitlines() if cron_line_base(ln)]
    if mine:
        acts.append((f"remove {len(mine)} crontab line(s) ttp added, keeping every other line:" + "".join(
            "\n    " + ("@reboot" if ln.startswith("@reboot") else " ".join(ln.split()[:5]))
            + f" ... {CRON_TAG}{cron_line_base(ln)}" for ln in mine), _drop_cron_lines))
    cron_bases = [cron_line_base(ln) for ln in mine]
    bases = {str(r / project.FOLDER): n for n, r in local_projects().items()}
    bases.update({b: bases.get(b, Path(b).parent.name) for b in cron_bases})
    for base, name in sorted(bases.items()):
        pidfile = Path(base) / "state" / "daemon.pid"
        pid = _read_pid(pidfile)
        if pid and _is_daemon(pid):
            acts.append((f"end the daemon of {name} (pid {pid}) if still running", _end_daemon(pidfile)))
    sc = shortcut.path()
    if sc.exists() and shortcut.ours(sc):
        acts.append((f"remove the /tt-project shortcut {sc}", lambda: shortcut.remove() and ""))
    shim = bin_dir / "ttp"
    if is_shim(shim):
        acts.append((f"remove the ttp command {shim}", lambda: shim.unlink(missing_ok=True) or ""))
    acts += [(f"remove the PATH line the installer added to {f}", _drop_path_line(f)) for f in _profiles() if _marked(f)]
    from . import notifier
    app = notifier.app_ready()
    if app:
        acts.append((f"remove the desktop notifier app {app}", lambda: shutil.rmtree(app, ignore_errors=True) or ""))
    lib = project.HOME_DIR / "lib"
    if lib.exists():
        acts.append((f"remove the installed lib {lib}", lambda: shutil.rmtree(lib, ignore_errors=True) or ""))
    return acts


def leftovers(bin_dir: Path) -> list[str]:
    """What uninstall leaves, each with the command that removes it by hand."""
    out = [f"project folder of {n}: {r / project.FOLDER} (its history; the project's own files stay too): "
           f"rm -rf {shlex.quote(str(r / project.FOLDER))}" for n, r in local_projects().items()
           if (r / project.FOLDER).is_dir()]
    shim = bin_dir / "ttp"
    if shim.exists() and not is_shim(shim):
        out.append(f"{shim}: not the one ttp setup wrote")
    if project.HOME_DIR.exists():
        out.append(f"per-user files (project list, saved keys, settings): rm -rf {shlex.quote(str(project.HOME_DIR))}")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ttp"
    if cache.exists():
        out.append(f"ttp's cache: rm -rf {shlex.quote(str(cache))}")
    if sys.platform.startswith("linux") and shutil.which("loginctl"):
        out.append("linger (lets user services run without a login), unchanged: "
                   "loginctl disable-linger, if nothing else of yours needs it")
    out.append(f"the {MARKETPLACE} marketplace registration in Claude Code: "
               f"claude plugin marketplace remove {MARKETPLACE}  (also uninstalls its plugins)")
    plugins = claude_config_dir() / "plugins" / "cache" / MARKETPLACE
    if plugins.exists():
        out.append(f"Claude Code's plugin cache: rm -rf {shlex.quote(str(plugins))}")
    if shutil.which("codex"):
        out.append(f"Codex: codex plugin remove tt-project@{MARKETPLACE} && codex plugin marketplace remove {MARKETPLACE}")
    cursor = Path.home() / ".cursor" / "plugins" / "local" / "tt-project"
    if cursor.exists():
        out.append(f"Cursor: rm -rf {shlex.quote(str(cursor))}")
    clone = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "tt-project"
    if clone.exists():
        out.append(f"the one-line install's clone: rm -rf {shlex.quote(str(clone))}")
    return out
