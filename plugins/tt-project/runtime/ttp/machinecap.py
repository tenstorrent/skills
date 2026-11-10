# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One machine-wide limit on concurrent workers across every tt-project project on a user's own
laptop or workstation, so many projects together cannot overheat it or starve a call.

Each project's budget.max_parallel_workers stays its own ceiling; this limit caps the sum of all
projects' running workers on the machine, so no project cap can go past it. It is paced by measured
signals, read at most once per SIGNAL_S for the whole machine and shared through the state file:
  - CPU: the busy share of the cores (hostcpu.busy_cores);
  - memory: the free share (hostcpu.mem_free), macOS's memory-pressure level and compressor size,
    Linux's memory PSI (/proc/pressure/memory);
  - a call: macOS power assertions held by a call app (`pmset -g assertions`; only process and
    assertion names are read, and only a yes/no is kept);
  - heat: macOS's thermal pressure level (notify key com.apple.system.thermalpressurelevel),
    Linux thermal zones.
Each busy signal multiplies the base by its factor (FACTORS); the limit never drops below 1. An
unreadable CPU or memory signal counts as moderately busy; an unreadable call or heat signal as none.
The limit is never unlimited.

Config (provenance as any key: a value the user or coordinator set is kept across upgrades):
  machine.worker_limit: auto (default; on for laptops and workstations: macOS or a battery present,
    off on lab and build servers), on, off.
  machine.max_workers: the base before pacing; null (default) is half the cores, from 2 to 8.
Undo: `ttp config <project> machine.worker_limit off` (each project).

State: TTP_HOME/machine-workers.json, under an fcntl lock (.lock beside it), shared by all projects:
each project's daemon pid, running workers and limit, and the last signal reading. The limit in
force is the lowest a live project publishes. A daemon publishes its running count each dispatch
pass and claims a place atomically before each start (claim), so two projects starting in the same
second cannot both take the last place, and one tick starting all ready work stays under it.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import hostcpu

STATE = "machine-workers.json"
SIGNAL_S = 30.0     # signals are read at most this often per machine
STALE_S = 900.0     # a project entry not refreshed this long no longer counts
BASE_MIN, BASE_MAX = 2, 8
FACTORS = {"moderate": 0.75, "heavy": 0.5}
# processes whose display-sleep assertion means a call is on (macOS); names only, matched in any case
CALL_APPS = ("zoom.us", "microsoft teams", "msteams", "webex", "avconferenced", "facetime", "slack")
CALL_WORDS = re.compile(r"webrtc|in a meeting|in a call|huddle", re.I)
DARWIN = sys.platform == "darwin"


def _home() -> Path:
    return Path(os.environ.get("TTP_HOME", Path.home() / ".tt-project"))


def has_battery(root: Path = Path("/sys/class/power_supply")) -> bool:
    try:
        return any((d / "type").read_text().strip() == "Battery" for d in root.iterdir())
    except OSError:
        return False


def enabled(cfg: dict, darwin: bool | None = None, battery=None) -> bool:
    """machine.worker_limit: on/off as set; auto (or unreadable) on for macOS or a battery present."""
    v = str(((cfg or {}).get("machine") or {}).get("worker_limit", "auto")).strip().lower()
    if v in ("on", "true", "1", "yes"):
        return True
    if v in ("off", "false", "0", "no"):
        return False
    return (DARWIN if darwin is None else darwin) or (battery or has_battery)()


def base(cfg: dict, cpus: int | None = None) -> int:
    """machine.max_workers when set (at least 1), else half the cores within BASE_MIN..BASE_MAX."""
    v = ((cfg or {}).get("machine") or {}).get("max_workers")
    if v is not None and not isinstance(v, bool):
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            pass
    return max(BASE_MIN, min(BASE_MAX, (cpus or hostcpu.cores()) // 2))


def levels(sig: dict) -> dict:
    """{signal: 'moderate' | 'heavy'} for each busy signal in a reading (read_signals' keys)."""
    out = {}
    b = sig.get("cpu_busy")
    if b is None:
        out["cpu"] = "moderate"   # unreadable: assume busy enough to slow down
    elif b >= 0.9:
        out["cpu"] = "heavy"
    elif b >= 0.75:
        out["cpu"] = "moderate"
    free, press, comp, psi = (sig.get(k) for k in ("mem_free", "pressure", "compressor", "psi"))
    if all(x is None for x in (free, press, comp, psi)):
        out["memory"] = "moderate"
    elif (free is not None and free < 0.10) or (press or 0) >= 4 or (comp or 0) >= 0.5 or (psi or 0) >= 20:
        out["memory"] = "heavy"
    elif (free is not None and free < 0.20) or (press or 0) >= 2 or (comp or 0) >= 0.3 or (psi or 0) >= 5:
        out["memory"] = "moderate"
    if sig.get("call"):
        out["call"] = "heavy"
    t = sig.get("thermal")
    if t is not None and t >= 2:
        out["thermal"] = "heavy"
    elif t is not None and t >= 1:
        out["thermal"] = "moderate"
    return out


def limit(top: int, sig: dict) -> int:
    """The machine-wide worker limit from base `top` and a signal reading: never under 1."""
    f = math.prod(FACTORS[v] for v in levels(sig).values())
    return max(1, math.floor(top * f + 1e-9))


def words(sig: dict) -> str:
    lv = levels(sig)
    return ", ".join(f"{k} {v}" for k, v in lv.items()) or "nothing busy"


# -- signal readers (each returns None where it cannot read) --------------------------------------

def _sysctl_u64(name: bytes) -> int | None:
    lib, _ = hostcpu._libsystem()
    if lib is None:
        return None
    import ctypes
    v, n = ctypes.c_uint64(0), ctypes.c_size_t(8)
    if lib.sysctlbyname(name, ctypes.byref(v), ctypes.byref(n), None, 0) != 0:
        return None
    return v.value & ((1 << (8 * n.value)) - 1) if n.value < 8 else v.value


def mac_thermal() -> int | None:
    """macOS thermal pressure: 0 nominal, 1 moderate, 2 heavy, 3+ trapping or worse."""
    lib, _ = hostcpu._libsystem()
    if lib is None:
        return None
    import ctypes
    tok, st = ctypes.c_int(0), ctypes.c_uint64(0)
    if lib.notify_register_check(b"com.apple.system.thermalpressurelevel", ctypes.byref(tok)) != 0:
        return None
    try:
        return int(st.value) if lib.notify_get_state(tok, ctypes.byref(st)) == 0 else None
    finally:
        lib.notify_cancel(tok)


def linux_thermal(root: Path = Path("/sys/class/thermal")) -> int | None:
    """The hottest thermal zone as a level: 2 at 90 C or more, 1 at 80 C or more, else 0."""
    try:
        temps = [int((z / "temp").read_text()) / 1000 for z in root.glob("thermal_zone*")]
    except (OSError, ValueError):
        return None
    if not temps:
        return None
    hot = max(temps)
    return 2 if hot >= 90 else 1 if hot >= 80 else 0


def psi(path: Path = Path("/proc/pressure/memory")) -> float | None:
    try:
        m = re.search(r"^some avg10=([\d.]+)", path.read_text(), re.M)
    except OSError:
        return None
    return float(m.group(1)) if m else None


def in_call(text: str | None = None) -> bool | None:
    """True when a call app holds a display-sleep assertion (macOS `pmset -g assertions`)."""
    if text is None:
        try:
            text = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True,
                                  timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    for ln in text.splitlines():
        m = re.match(r"\s*pid \d+\((.+?)\):.*?(DisplaySleep\w*)(.*)", ln)
        if m and ("NoDisplaySleep" in ln or "PreventUserIdleDisplaySleep" in ln) and (
                any(a in m.group(1).lower() for a in CALL_APPS) or CALL_WORDS.search(m.group(3))):
            return True
    return False


def read_signals(darwin: bool | None = None) -> dict:
    darwin = DARWIN if darwin is None else darwin
    busy = hostcpu.busy_cores()
    sig = {"at": time.time(), "cpu_busy": None if busy is None else round(busy / max(hostcpu.cores(), 1), 3),
           "mem_free": hostcpu.mem_free()}
    if darwin:
        mem, comp = _sysctl_u64(b"hw.memsize"), _sysctl_u64(b"vm.compressor_bytes_used")
        sig.update(pressure=_sysctl_u64(b"kern.memorystatus_vm_pressure_level"),
                   compressor=round(comp / mem, 3) if mem and comp is not None else None,
                   call=in_call(), thermal=mac_thermal())
    else:
        sig.update(psi=psi(), thermal=linux_thermal())
    return sig


# -- shared state ----------------------------------------------------------------------------------

@contextlib.contextmanager
def _locked(home: Path | None = None):
    home = home or _home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / STATE
    with open(home / (STATE + ".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        data.setdefault("projects", {})
        yield data
        tmp = path.with_name(f".{STATE}.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, path)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError, OverflowError):
        return True
    return True


def _others(data: dict, project: str, now: float) -> tuple[int, list[int]]:
    """(running workers of the other live projects, the limits they publish)."""
    n, lims = 0, []
    for name, e in list(data["projects"].items()):
        if not isinstance(e, dict) or now - float(e.get("at") or 0) > STALE_S or not _alive(e.get("pid")):
            data["projects"].pop(name, None)
            continue
        if name != project:
            n += int(e.get("running") or 0)
            if e.get("limit"):
                lims.append(int(e["limit"]))
    return n, lims


def publish(project: str, cfg: dict, running: int, now: float | None = None, home: Path | None = None,
            pid: int | None = None) -> None:
    """Record this project's running workers (each dispatch pass, the limit on or off); a project with
    the limit off publishes no limit of its own."""
    now = time.time() if now is None else now
    with _locked(home) as data:
        e = data["projects"].get(project)
        e = e if isinstance(e, dict) and enabled(cfg) else {}
        data["projects"][project] = {**e, "pid": pid or os.getpid(), "at": now, "running": running}


def check(project: str, cfg: dict, running: int, claim: bool = False, now: float | None = None,
          home: Path | None = None, read=None, pid: int | None = None) -> str | None:
    """None when one more worker may start on the machine with this project's `running` ones, else why
    not. With `claim`, a None also records the start (running + 1) before the lock is released, so
    another project's check sees it. A project with the limit off is never held."""
    if not enabled(cfg):
        return None
    now = time.time() if now is None else now
    with _locked(home) as data:
        sig = data.get("signals") if isinstance(data.get("signals"), dict) else None
    if not sig or not 0 <= now - float(sig.get("at") or 0) < SIGNAL_S:
        sig = (read or read_signals)()   # outside the lock: the CPU sample may take a second
    with _locked(home) as data:
        others, lims = _others(data, project, now)
        data["signals"] = sig
        own = limit(base(cfg), data["signals"])
        top = min([own, *lims])
        if others + running >= top:
            return (f"the machine-wide worker limit: {others + running} of {top} running across all projects "
                    f"({words(data['signals'])}; machine.worker_limit)")
        if claim:
            data["projects"][project] = {"pid": pid or os.getpid(), "at": now, "running": running + 1,
                                         "limit": own}
        return None
