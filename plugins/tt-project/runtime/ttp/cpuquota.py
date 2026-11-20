# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A CPU-quota group around check and test commands, which tightens while the host's live service is busy.

nice and ionice do not protect a live service in another cgroup: cgroup v2 splits CPU between slices
by cpu.weight, so a suite niced to 19 in user.slice still takes its slice's share from a service in
system.slice. This runs every check command (testslots.low_priority) and every `ttp sandbox` test run
inside a group with a hard CPU limit: a systemd --user scope with CPUQuota, else the delegated cgroup v2
directory checks.cpu_cgroup names (a child group per run, with cpu.max).

Settings (the project's checks section, then the account's; off unless set, so nothing changes by default):
- checks.cpu_quota: cores the group may use while the host is not busy (unset: no limit).
- checks.busy_cpu_quota: cores while checks.busy_probe says busy (at most cpu_quota).
- checks.busy_probe: a shell command run every checks.busy_probe_s seconds (default PROBE_S); exit 0
  means a live service is busy, 1 idle, anything else (or a timeout) counts as busy.
- checks.cpu_cgroup: a cgroup v2 directory this account may write, used where systemd --user is missing.
For the quotas the smaller of the project's and the account's wins: a project may only tighten them.

The wrapper (main) starts the command in the group at the quota the probe gives now, probes again every
interval and changes the quota in place (systemctl --user set-property / cpu.max) when the answer
changes, and exits with the command's code; the command dies with it. A command already in a group (GROUP_ENV) gets no second one.
Where neither systemd --user with the cpu controller nor a usable cgroup directory exists, the command
runs as it is and one line says why (once per reason, not per run).
"""
from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

GROUP_ENV = "TTP_CPU_GROUP"   # set inside a group: a nested check or sandbox adds none
PROBE_S, PROBE_MIN_S, PROBE_TIMEOUT_S = 30.0, 1.0, 20.0
PERIOD_US = 100_000           # cpu.max's period
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _cores(v) -> float | None:
    if isinstance(v, bool) or v in (None, ""):
        return None
    try:
        n = float(str(v).strip())
    except ValueError:
        return None
    return n if n > 0 and math.isfinite(n) else None


def settings(cfg: dict | None, acct: dict | None) -> dict | None:
    """The group's settings from the project's config `cfg` and the account settings `acct` (both whole
    configs), or None when none applies: no quota set, or a busy probe without a busy quota and no other."""
    sec = [s if isinstance(s, dict) else {} for s in ((cfg or {}).get("checks"), (acct or {}).get("checks"))]
    idle = min([n for s in sec if (n := _cores(s.get("cpu_quota")))] or [math.inf])
    busy = min([n for s in sec if (n := _cores(s.get("busy_cpu_quota")))] or [math.inf])
    probe = next((s["busy_probe"].strip() for s in sec if isinstance(s.get("busy_probe"), str) and s["busy_probe"].strip()), "")
    every = next((n for s in sec if (n := _cores(s.get("busy_probe_s")))), PROBE_S)
    cgroup = next((s["cpu_cgroup"] for s in sec if isinstance(s.get("cpu_cgroup"), str) and s["cpu_cgroup"]), "")
    if not probe or busy == math.inf:
        probe, busy = "", idle
    if idle == busy == math.inf:
        return None
    return {"idle": None if idle == math.inf else idle, "busy": None if min(busy, idle) == math.inf else min(busy, idle),
            "probe": probe, "every": max(PROBE_MIN_S, every), "cgroup": cgroup}


def wrap(argv: list, conf: dict | None) -> list:
    """`argv` behind the wrapper (main) with settings `conf`, or as it is when `conf` is None or the
    command already runs in a group. The wrapper is started with -I, so the caller's directory and
    PYTHON* variables cannot change what it imports; the command keeps the full environment."""
    if conf is None or os.environ.get(GROUP_ENV):
        return list(argv)
    runtime = str(Path(__file__).resolve().parent.parent)
    boot = f"import sys; sys.path.insert(0, {runtime!r}); from ttp.cpuquota import main; sys.exit(main(sys.argv[1:]))"
    return [sys.executable, "-I", "-c", boot, json.dumps(conf), "--", *argv]


def busy(probe: str, timeout: float) -> bool:
    """Whether the busy probe says a live service is busy: exit 0 busy, 1 idle, anything else busy."""
    try:
        rc = subprocess.run(probe, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout).returncode
    except (OSError, subprocess.TimeoutExpired):
        return True
    return rc != 1


def _systemd() -> str | None:
    """Why no systemd --user scope can limit CPU here, or None when one can."""
    if not (shutil.which("systemd-run") and shutil.which("systemctl")):
        return "systemd-run or systemctl is missing"
    try:
        r = subprocess.run(["systemctl", "--user", "show", "-p", "ControlGroup", "--value"], capture_output=True,
                           text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return "systemctl --user did not answer"
    cg = r.stdout.strip()
    if r.returncode or not cg:
        return "no systemd --user manager"
    try:
        ctl = (CGROUP_ROOT / cg.lstrip("/") / "cgroup.controllers").read_text().split()
    except OSError:
        return f"no cgroup v2 controllers file for {cg}"
    return None if "cpu" in ctl else ("the cpu controller is not delegated to systemd --user; a user@.service drop-in with"
                                     " Delegate=cpu (or checks.cpu_cgroup) gives one")


def _cgroup(path: str) -> str | None:
    """Why the cgroup v2 directory `path` cannot hold a limited child group, or None when it can."""
    d = Path(path)
    try:
        ok = "cpu" in (d / "cgroup.subtree_control").read_text().split()
    except OSError:
        return f"{path} is no cgroup v2 directory"
    if not ok:
        return f"the cpu controller is not enabled in {path}/cgroup.subtree_control"
    return None if os.access(d, os.W_OK) else f"{path} is not writable"


def _quota_text(cores: float | None) -> str:
    return "no limit" if cores is None else f"{cores:g} core{'' if cores == 1 else 's'}"


def _say_once(why: str) -> None:
    """Say on stderr why the command runs without a group, once per reason on this account."""
    mark = Path(os.environ.get("TTP_HOME") or Path.home() / ".tt-project") / "cpuquota-fallback.txt"
    try:
        if mark.read_text() == why:
            return
    except OSError:
        pass
    print(f"ttp: no CPU-quota group ({why}): checks run without one", file=sys.stderr, flush=True)
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(why)
    except OSError:
        pass


def _die_with_parent(then=None):
    """A preexec_fn: the command gets SIGKILL from the kernel if this wrapper dies (Linux), so a killed
    wrapper leaves nothing running; `then` runs after."""
    try:
        import ctypes
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        prctl = None
    parent = os.getpid()

    def apply() -> None:
        if prctl is not None:
            prctl(1, signal.SIGKILL)   # PR_SET_PDEATHSIG
            if os.getppid() != parent:
                os._exit(1)
        if then is not None:
            then()
    return apply


def _exec(argv: list) -> int:
    """Run `argv` in place of this process, as it would run without a group."""
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        print(f"ttp: {argv[0]}: {e.strerror}", file=sys.stderr)
        return 127 if isinstance(e, FileNotFoundError) else 126


def _rmdir(d: Path) -> None:
    try:
        d.rmdir()
    except OSError:
        pass


def main(args: list) -> int:
    """The wrapper: `<settings json> -- <command...>`. Exits with the command's code (128 + the signal)."""
    conf, argv = json.loads(args[0]), args[2:]
    probe, every = conf.get("probe") or "", float(conf.get("every") or PROBE_S)
    is_busy = bool(probe) and busy(probe, PROBE_TIMEOUT_S)
    quota = conf.get("busy") if is_busy else conf.get("idle")
    name = f"ttp-checks-{os.getpid()}-{time.time_ns() % 10**9}"
    env = {**os.environ, GROUP_ENV: name}
    why = _systemd()
    child = None
    if why is None:
        unit = f"{name}.scope"
        pct = [] if quota is None else ["-p", f"CPUQuota={round(quota * 100)}%"]
        cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", *pct, "--", *argv]

        def apply(cores):
            v = "" if cores is None else f"{round(cores * 100)}%"
            try:
                return subprocess.run(["systemctl", "--user", "set-property", "--runtime", unit, f"CPUQuota={v}"],
                                      stdin=subprocess.DEVNULL, capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False   # tried again at the next probe
        proc = subprocess.Popen(cmd, env=env, preexec_fn=_die_with_parent())
    elif conf.get("cgroup") and (cwhy := _cgroup(conf["cgroup"])) is None:
        child = Path(conf["cgroup"]) / name

        def apply(cores):
            v = "max" if cores is None else str(max(1000, round(cores * PERIOD_US)))
            try:
                (child / "cpu.max").write_text(f"{v} {PERIOD_US}\n")
                return True
            except OSError:
                return False
        try:
            child.mkdir()
        except OSError as e:
            _say_once(f"cannot make {child}: {e.strerror}")
            return _exec(argv)
        apply(quota)
        procs = str(child / "cgroup.procs")
        err_r, err_w = os.pipe()   # why the join failed, from the forked child

        def join():
            try:
                with open(procs, "w") as f:
                    f.write("0")
            except OSError as e:
                os.write(err_w, (e.strerror or str(e)).encode()[:200])
                raise
        try:
            proc = subprocess.Popen(argv, env=env, preexec_fn=_die_with_parent(join))
        except (OSError, subprocess.SubprocessError) as e:
            # the command never ran: drop the empty group and run it without one
            os.close(err_w)
            joined = os.read(err_r, 256).decode(errors="replace")
            os.close(err_r)
            _rmdir(child)
            if joined or not isinstance(e, OSError):
                _say_once(f"cannot join a group in {conf['cgroup']}: {joined or e}")
            return _exec(argv)
        os.close(err_w)
        os.close(err_r)
    else:
        _say_once(why + (f"; {cwhy}" if conf.get("cgroup") else ""))
        return _exec(argv)

    def forward(sig, _):
        try:
            proc.send_signal(sig)
        except OSError:
            pass
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, forward)
    applied = quota
    print(f"ttp: checks CPU quota {_quota_text(quota)}{' (host busy)' if is_busy else ''}", file=sys.stderr, flush=True)
    while True:
        try:
            rc = proc.wait(timeout=every if probe else None)
            break
        except subprocess.TimeoutExpired:
            is_busy = busy(probe, PROBE_TIMEOUT_S)
            quota = conf.get("busy") if is_busy else conf.get("idle")
            if quota != applied and apply(quota):
                applied = quota
                print(f"ttp: host {'busy' if is_busy else 'idle'}: checks CPU quota now {_quota_text(quota)}",
                      file=sys.stderr, flush=True)
    if child is not None:
        _rmdir(child)
    return rc if rc >= 0 else 128 - rc
