# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A test run the harness starts never changes the host's real crontab, systemd user units or power state.

Every check `ttp checks` and the push queue run (push.check_env), every `ttp sandbox` command and the
plugin's own test sessions (tests/conftest.py) get three things, with no namespace or sandbox:

- Stand-ins first on PATH (hostshim.py) for crontab, systemctl, loginctl, at, atq, atrm, batch,
  reboot, shutdown, poweroff, halt, ipmitool and tt-smi. A write goes to a per-run fake store and is
  recorded there; a read is served from it, or by the real command where the store has no answer.
  The writes are listed after the run, which they do not fail. A sudo stand-in refuses (exit 1, also
  listed) sudo running one of those as a write, or a copy into a systemd unit folder or the cron
  spool: sudo resets PATH, so the stand-ins would never see it. Other sudo calls go to the real sudo.
- The marker TTP_HOSTSTATE_FAKE naming that store. The harness's own installers (service.py,
  uninstall.py) honour it while HOME is the account's real home: their units go to
  <store>/systemd/user and their crontab, systemctl and loginctl calls to the stand-ins.
- A snapshot of the real user crontab and ~/.config/systemd/user (files and symlinks) before and after
  the run. A change the stand-ins missed (a call by absolute path, a unit file written directly)
  fails the run naming it; each changed line or file is put back as it was, and the restore is
  recorded in the machine change ledger (as a test change that closes itself).

The snapshot leaves out the harness's own entries (crontab lines tagged `# tt-project:`, units named
tt-project-*), which its daemons rewrite while tests run; under the marker its installers never touch
the real ones. Only files are compared, not which units run: a unit started (not enabled) by
absolute path is not seen."""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

MARKER = "TTP_HOSTSTATE_FAKE"
NAMES = ("crontab", "systemctl", "loginctl", "at", "atq", "atrm", "batch", "reboot", "shutdown",
         "poweroff", "halt", "ipmitool", "tt-smi", "sudo")
SHIMS = ".ttp-shims"   # in each stand-in folder, so a nested run and the stand-ins skip every such folder
OWN_CRON = "# tt-project:"
OWN_UNIT = "tt-project-"
MAX_UNIT_BYTES = 1 << 20


def real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _real_dir() -> Path:
    return real_home() / ".config" / "systemd" / "user"


def make(store: Path) -> Path:
    """Write the stand-ins into <store>/bin (again only where missing); returns that folder."""
    bin_dir = store / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    prog = bin_dir / "_hostshim.py"   # a copy: a plugin upgrade during the run leaves it in place
    if not prog.exists():
        shutil.copyfile(Path(__file__).with_name("hostshim.py"), str(prog) + ".tmp")
        os.replace(str(prog) + ".tmp", prog)
    for name in NAMES:
        if not (f := bin_dir / name).exists():
            f.with_suffix(".tmp").write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I "
                                             f"{shlex.quote(str(prog))} {shlex.quote(str(store))} {name} \"$@\"\n")
            os.chmod(f.with_suffix(".tmp"), 0o755)
            os.replace(f.with_suffix(".tmp"), f)
    (bin_dir / SHIMS).touch()
    return bin_dir


def fake_env(env: dict, store: str | Path | None = None) -> dict:
    """`env` with the stand-ins first on PATH and MARKER naming their store: the one `env` already names
    (a nested run), else `store`, else a new temp folder."""
    store = Path(env.get(MARKER) or store or tempfile.mkdtemp(prefix="ttp-hoststate-"))
    try:
        bin_dir = make(store)
    except OSError:
        return env
    rest = [d for d in env.get("PATH", os.defpath).split(os.pathsep) if d and d != str(bin_dir)]
    return {**env, MARKER: str(store), "PATH": os.pathsep.join([str(bin_dir), *rest])}


def _faked() -> Path | None:
    """The store, when the marker is set and HOME is the real home (a test's own temp HOME is its fake)."""
    store = os.environ.get(MARKER)
    try:
        return Path(store) if store and Path.home().resolve() == real_home().resolve() else None
    except (OSError, KeyError):
        return None


def unit_dir() -> Path:
    """Where an installer writes systemd user units: the store's under the marker, else ~/.config/systemd/user."""
    store = _faked()
    return store / "systemd" / "user" if store else Path.home() / ".config" / "systemd" / "user"


def argv(args) -> list[str]:
    """An installer's command, its crontab, systemctl or loginctl the stand-in's under the marker."""
    args = list(args)
    if (store := _faked()) and args and args[0] in NAMES:
        args[0] = str(make(store) / args[0])
    return args


def _real(name: str) -> str | None:
    path = os.pathsep.join(d for d in os.environ.get("PATH", os.defpath).split(os.pathsep)
                           if d and not os.path.exists(os.path.join(d, SHIMS)))
    return shutil.which(name, path=path + os.pathsep + "/usr/bin:/bin")


_POPEN = subprocess.Popen   # its own probes: never counted as, or stopped like, the run's commands


def _run(cmd: list[str], input: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    """subprocess.run for the guard's own crontab and systemctl calls, on the Popen of import time."""
    with _POPEN(cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as p:
        try:
            out, err = p.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def _crontab() -> str | None | bool:
    """The real crontab's text; None when there is none; False when it cannot be read."""
    if not (real := _real("crontab")):
        return False
    try:
        r = _run([real, "-l"])
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout if r.returncode == 0 else None if "no crontab" in r.stderr.lower() else False


def _units(top: Path) -> dict:
    found = {}
    for root, dirs, files in os.walk(top):
        for n in dirs + files:
            p = Path(root) / n
            rel = str(p.relative_to(top))
            try:
                if p.is_symlink():
                    found[rel] = ("link", os.readlink(p))
                elif p.is_file():
                    data = p.read_bytes() if p.stat().st_size <= MAX_UNIT_BYTES else b""
                    found[rel] = ("file", hashlib.sha256(data).hexdigest(), data)
                elif p.is_dir():   # a new .wants folder goes too
                    found[rel] = ("dir", "")
            except OSError:
                pass
    return found


def snapshot() -> dict:
    return {"crontab": _crontab(), "units": _units(_real_dir())}


def _lines(text) -> list[str]:
    return [ln for ln in (text or "").splitlines() if OWN_CRON not in ln]


def diff(before: dict, after: dict) -> dict:
    """What changed outside the harness's own entries: crontab lines added and removed, unit paths."""
    out = {}
    if before["crontab"] is not False and after["crontab"] is not False:
        b, a = _lines(before["crontab"]), _lines(after["crontab"])
        added, removed = list((Counter(a) - Counter(b)).elements()), list((Counter(b) - Counter(a)).elements())
        if added or removed:
            out["crontab"] = (added, removed)
    units = sorted(r for r in set(before["units"]) | set(after["units"])
                   if before["units"].get(r, ())[:2] != after["units"].get(r, ())[:2]
                   and not Path(r).name.startswith(OWN_UNIT))
    if units:
        out["units"] = units
    return out


def restore(before: dict, d: dict) -> list[str]:
    """Put each changed crontab line and unit path back as `before` had it, on top of what is there now
    (a concurrent change elsewhere stays). Returns the problems."""
    errors = []
    if "crontab" in d:
        added, removed = d["crontab"]
        cur = _crontab()
        if cur is False:
            errors.append("the crontab could not be read back")
        else:
            drop, keep = Counter(added), []
            for ln in reversed((cur or "").splitlines()):   # the last copies of an added line go
                if drop[ln] > 0:
                    drop[ln] -= 1
                else:
                    keep.insert(0, ln)
            keep += list((Counter(removed) - Counter(keep)).elements())
            real = _real("crontab")
            r = _run([real, "-"], input="".join(f"{ln}\n" for ln in keep)) \
                if keep or before["crontab"] is not None else _run([real, "-r"])
            if r.returncode:
                errors.append(f"crontab: {r.stderr.strip()[:200]}")
    top = _real_dir()
    for rel in reversed(d.get("units", [])):   # deepest first, so a folder is empty when it is removed
        p, was = top / rel, before["units"].get(rel)
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir() and not was:
                p.rmdir()
            if was and was[0] == "dir":
                p.mkdir(parents=True, exist_ok=True)
            elif was and was[0] == "link":
                p.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(was[1], p)
            elif was:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(was[2])
        except OSError as e:
            errors.append(f"{rel}: {e.strerror or e}")
    if d.get("units") and (sc := _real("systemctl")):
        _run([sc, "--user", "daemon-reload"], timeout=60)
    return errors


def describe(d: dict) -> str:
    parts = []
    if "crontab" in d:
        added, removed = d["crontab"]
        parts += [f"crontab line added: {ln[:120]!r}" for ln in added[:5]]
        parts += [f"crontab line removed: {ln[:120]!r}" for ln in removed[:5]]
    parts += [f"systemd user unit path changed: {r}" for r in d.get("units", [])[:10]]
    return "; ".join(parts)


def record(what: str) -> str:
    """The restore in the machine change ledger, as a test change that closes itself; what was said."""
    try:
        from . import machine_ledger, machines
        here = machines.here()
        if not here:
            return "not recorded in the machine change ledger: this host is not in `ttp machines list`"
        task = os.environ.get("TTP_TASK", "")
        c = machine_ledger.add_change(here[0], what, "none: the harness restored it after the test run",
                                      os.environ.get("TTP_PROJECT") or "tt-project", expires=time.time() + 600,
                                      task=int(task) if task.isdigit() else None, test=True)
        return f"recorded in the machine change ledger ({here[0]}, change {c.get('id', c.get('queued', '?'))})"
    except Exception as e:   # the restore stands; only its record failed
        return f"not recorded in the machine change ledger: {e}"


def faked_writes(store, since: float) -> list[str]:
    """The writes the stand-ins took since `since`, one line each (the same call counted)."""
    seen: dict[str, int] = {}
    try:
        lines = Path(store, "calls.jsonl").read_text().splitlines() if store else []
    except OSError:
        lines = []
    for ln in lines:
        try:
            c = json.loads(ln)
        except ValueError:
            continue
        if c.get("write") and float(c.get("ts") or 0) >= since:
            k = shlex.join([c["cmd"], *c["argv"]])[:160]
            seen[k] = seen.get(k, 0) + 1
    return [k + (f" ({n} times)" if n > 1 else "") for k, n in seen.items()]


class Guard:
    """Snapshot on creation; end() compares, restores and says what happened."""

    def __init__(self, env: dict | None = None):
        self.store = (env if env is not None else os.environ).get(MARKER)
        self.since = time.time()
        self.before = snapshot()
        self.done = False

    def end(self) -> tuple[bool, list[str]]:
        """(the real state changed, the lines to report)."""
        self.done = True
        said = []
        if writes := faked_writes(self.store, self.since):
            said.append("host guard: these calls went to the run's fake store, never the host: " + "; ".join(writes[:10]))
        d = diff(self.before, snapshot())
        if d:
            what = describe(d)
            errors = restore(self.before, d)
            ledger = record(f"a test run changed the real {what[:300]} (a call by path the stand-ins missed); "
                            "the harness put it back")
            said.append(f"host guard: FAILED: the run changed the host's real state: {what}. "
                        + (f"Restore problems: {'; '.join(errors)}" if errors else "Restored as it was before the run")
                        + f"; {ledger}")
        return bool(d), said

    def stopped(self, say) -> None:
        """end() on a stopped or failed run's path, once its command is killed and reaped: each line to
        `say`. Without it a change made before the stop would be the next run's baseline. A fault here
        never masks the stop."""
        if self.done:
            return
        try:
            for line in self.end()[1]:
                say(line)
        except Exception:
            pass
