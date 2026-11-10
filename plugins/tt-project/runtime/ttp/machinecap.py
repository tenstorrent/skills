# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One machine-wide limit on concurrent workers across every tt-project project on a user's own
laptop or workstation, so many projects together cannot overheat it or starve a call.

Each project's budget.max_parallel_workers stays its own ceiling; this limit caps the sum of all
projects' running workers on the machine, so no project cap can go past it. Workers are agents: they
use little CPU and dispatch suites, builds and device jobs to other boxes. So the limit is cut only
when the machine itself is truly short, from signals read at most once per SIGNAL_S for the whole
machine and shared through the state file (the load average and short spikes never count):
  - CPU: idle under CPU_IDLE_LOW (hostcpu.busy_cores, the kernel's CPU ticks) in every reading for
    SUSTAIN_S or more: moderate, heavy when idle is also under CPU_IDLE_HEAVY now; one low sample
    is not short;
  - memory: macOS's memory-pressure level warn (moderate) or critical (heavy), swap growing by
    SWAP_GROW_B or more between readings (moderate), free memory under MEM_FREE_LOW (moderate) or
    MEM_FREE_HEAVY (heavy), Linux's memory PSI (/proc/pressure/memory). macOS compressing memory or
    reporting 'moderate' while it is still well free is normal and counts for nothing;
  - a call: macOS power assertions held by a call app (`pmset -g assertions`; only process and
    assertion names are read, and only a yes/no is kept), heavy only while CPU idle is under
    CALL_IDLE_LOW too; a call alone counts for nothing;
  - heat: thermal pressure serious or worse (macOS's level 2+, notify key
    com.apple.system.thermalpressurelevel; a Linux zone at 90 C or more): heavy.
Each busy signal multiplies the base by its factor (FACTORS); the limit never drops below 1. An
unreadable signal counts as not busy, so the limit holds at its base.

While no signal is busy, only a base someone set (machine.max_workers, in any live project) caps the
machine; with none set, each project's own caps alone apply. Under pressure the default base is the
cores, so a project left at null never drags the machine below another project's setting.

Config (provenance as any key: a value the user or coordinator set is kept across upgrades):
  machine.worker_limit: auto (default; on for laptops and workstations: macOS or a battery present,
    off on lab and build servers), on, off.
  machine.max_workers: the base before pacing; null (default) holds nothing while no signal is busy
    and is the cores (BASE_MIN to BASE_MAX) under pressure.
Undo: `ttp config <project> machine.worker_limit off` (each project).

State: TTP_HOME/machine-workers.json, under an fcntl lock (.lock beside it), shared by all projects:
each project's daemon pid, running workers, the base it set (null: none) and its limit, and the
last signal reading with the CPU's low-idle start and swap growth carried between readings. The base
in force is the lowest one a live project set; an older daemon's entry without one counts by its
published limit. A daemon publishes its running count each dispatch
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
BASE_MIN, BASE_MAX = 2, 32
CPU_IDLE_LOW = 0.15     # CPU idle under this in every reading for SUSTAIN_S: CPU moderate
CPU_IDLE_HEAVY = 0.05   # ... and under this now as well: CPU heavy
SUSTAIN_S = 60.0        # how long CPU idle must stay low before it counts
SUSTAIN_GAP_S = 120.0   # readings further apart than this start the low-idle window again
CALL_IDLE_LOW = 0.30    # a call counts only while CPU idle is under this
MEM_FREE_LOW, MEM_FREE_HEAVY = 0.10, 0.05
PRESSURE_WARN, PRESSURE_CRITICAL = 2, 4   # kern.memorystatus_vm_pressure_level (1 is normal)
SWAP_GROW_B = 64 << 20  # swap used (macOS) or swapped out (Linux) growing this much between readings
SWAP_GAP_S = 600.0      # readings further apart than this tell nothing about swapping
THERMAL_SERIOUS = 2     # macOS thermal pressure 2 (heavy, 'serious') or worse; Linux 90 C
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


def set_base(cfg: dict) -> int | None:
    """machine.max_workers when set to a number (at least 1), else None."""
    v = ((cfg or {}).get("machine") or {}).get("max_workers")
    if v is not None and not isinstance(v, bool):
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            pass
    return None


def base(cfg: dict, cpus: int | None = None) -> int:
    """machine.max_workers when set (at least 1), else the cores within BASE_MIN..BASE_MAX."""
    v = set_base(cfg)
    return v if v is not None else max(BASE_MIN, min(BASE_MAX, cpus or hostcpu.cores()))


def levels(sig: dict) -> dict:
    """{signal: 'moderate' | 'heavy'} for each busy signal in a reading (read_signals' keys, carried on
    by advance). An unreadable signal is not busy."""
    out = {}
    b, at, since = sig.get("cpu_busy"), sig.get("at"), sig.get("cpu_low_since")
    if b is not None and b > 1 - CPU_IDLE_LOW and since is not None and at is not None \
            and float(at) - float(since) >= SUSTAIN_S:
        out["cpu"] = "heavy" if b > 1 - CPU_IDLE_HEAVY else "moderate"
    free, press, psi = (sig.get(k) for k in ("mem_free", "pressure", "psi"))
    if (free is not None and free < MEM_FREE_HEAVY) or (press or 0) >= PRESSURE_CRITICAL or (psi or 0) >= 20:
        out["memory"] = "heavy"
    elif (free is not None and free < MEM_FREE_LOW) or (press or 0) >= PRESSURE_WARN or (psi or 0) >= 5 \
            or sig.get("swapping"):
        out["memory"] = "moderate"
    if sig.get("call") and b is not None and b > 1 - CALL_IDLE_LOW:
        out["call"] = "heavy"
    t = sig.get("thermal")
    if t is not None and t >= THERMAL_SERIOUS:
        out["thermal"] = "heavy"
    return out


def advance(prev: dict | None, sig: dict) -> dict:
    """A new reading with what only a series shows: `cpu_low_since` (when CPU idle went under
    CPU_IDLE_LOW in every reading since, readings at most SUSTAIN_GAP_S apart) and `swapping` (swap grew
    SWAP_GROW_B or more since a reading at most SWAP_GAP_S old)."""
    sig = dict(sig)
    prev = prev if isinstance(prev, dict) else {}
    at = float(sig.get("at") or time.time())
    try:
        gap = at - float(prev.get("at"))
    except (TypeError, ValueError):
        gap = None
    b = sig.get("cpu_busy")
    sig["cpu_low_since"] = None
    if b is not None and b > 1 - CPU_IDLE_LOW:
        held = prev.get("cpu_low_since")
        sig["cpu_low_since"] = held if held is not None and gap is not None and 0 <= gap <= SUSTAIN_GAP_S else at
    sw, psw = sig.get("swap"), prev.get("swap")
    sig["swapping"] = bool(sw is not None and psw is not None and gap is not None and 0 < gap <= SWAP_GAP_S
                           and sw - psw >= SWAP_GROW_B)
    return sig


def limit(top: int, sig: dict) -> int:
    """The machine-wide worker limit from base `top` and a signal reading: never under 1."""
    f = math.prod(FACTORS[v] for v in levels(sig).values())
    return max(1, math.floor(top * f + 1e-9))


def words(sig: dict) -> str:
    lv = levels(sig)
    return ", ".join(f"{k} {v}" for k, v in lv.items()) or "nothing busy"


def in_force(own: int | None, lims: list[int], sig: dict, cpus: int | None = None) -> int | None:
    """The machine-wide limit from this project's set base `own`, the bases other live projects set
    (`lims`) and a reading: None (no machine hold) while nothing is busy and no base is set."""
    bases = [x for x in (own, *lims) if x]
    if not levels(sig):
        return min(bases) if bases else None
    return limit(min(bases) if bases else base({}, cpus), sig)


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


def mac_swap() -> int | None:
    """macOS swap in use, bytes (sysctl vm.swapusage: total, avail, used as u64, then pagesize)."""
    lib, _ = hostcpu._libsystem()
    if lib is None:
        return None
    import ctypes
    buf, n = (ctypes.c_uint64 * 4)(), ctypes.c_size_t(32)
    if lib.sysctlbyname(b"vm.swapusage", buf, ctypes.byref(n), None, 0) != 0 or n.value < 24:
        return None
    return int(buf[2])


def linux_swap(path: Path = Path("/proc/vmstat")) -> int | None:
    """Bytes swapped out since boot (pswpout pages of 4 KiB), or None."""
    try:
        m = re.search(r"^pswpout (\d+)", path.read_text(), re.M)
    except OSError:
        return None
    return int(m.group(1)) * 4096 if m else None


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
                   call=in_call(), thermal=mac_thermal(), swap=mac_swap())
    else:
        sig.update(psi=psi(), thermal=linux_thermal(), swap=linux_swap())
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
    """(running workers of the other live projects, the bases they set; an older daemon's entry
    without a base counts by its published limit)."""
    n, lims = 0, []
    for name, e in list(data["projects"].items()):
        if not isinstance(e, dict) or now - float(e.get("at") or 0) > STALE_S or not _alive(e.get("pid")):
            data["projects"].pop(name, None)
            continue
        if name != project:
            n += int(e.get("running") or 0)
            got = e.get("base") if "base" in e else e.get("limit")
            if got:
                lims.append(int(got))
    return n, lims


def publish(project: str, cfg: dict, running: int, now: float | None = None, home: Path | None = None,
            pid: int | None = None) -> None:
    """Record this project's running workers (each dispatch pass, the limit on or off). A project with
    the limit on also records its set base and its limit now (from the stored reading, none read here),
    so a lower machine.max_workers binds the others before it starts anything; with it off, neither."""
    now = time.time() if now is None else now
    on = enabled(cfg)
    with _locked(home) as data:
        own = {}
        if on:
            sig = data.get("signals") if isinstance(data.get("signals"), dict) else {}
            mine = set_base(cfg)
            own = {"base": mine, "limit": in_force(mine, [], sig)}
        data["projects"][project] = {"pid": pid or os.getpid(), "at": now, "running": running, **own}


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
    fresh = not sig or not 0 <= now - float(sig.get("at") or 0) < SIGNAL_S
    if fresh:
        sig = (read or read_signals)()   # outside the lock: the CPU sample may take a second
    with _locked(home) as data:
        others, lims = _others(data, project, now)
        if fresh:
            prev = data.get("signals")
            if isinstance(prev, dict) and float(prev.get("at") or 0) >= float(sig.get("at") or 0):
                sig = prev   # another project stored a newer reading meanwhile
            else:
                data["signals"] = sig = advance(prev, sig)
        mine = set_base(cfg)
        top = in_force(mine, lims, sig)
        if top is not None and others + running >= top:
            return (f"the machine-wide worker limit: {others + running} of {top} running across all projects "
                    f"({words(sig)}; machine.worker_limit)")
        if claim:
            data["projects"][project] = {"pid": pid or os.getpid(), "at": now, "running": running + 1,
                                         "base": mine, "limit": top}
        return None
