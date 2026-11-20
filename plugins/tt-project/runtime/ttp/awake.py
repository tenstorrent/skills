"""Keep a Mac from sleeping while its project has work.

On macOS only, the daemon holds a sleep assertion (`caffeinate -i -s -w <daemon pid>`) while all of
these hold: the project has work (a running run or task, or queued work that can start now), the
machine is on AC power, and `runner.prevent_idle_sleep` is set to on or auto (the default, off, never
holds one: the user keeps the Mac awake themselves as needed). It lets go as soon as one stops
holding, and on shutdown. `-i` blocks idle sleep; `-s` (PreventSystemSleep, honoured on AC power only)
also blocks the system sleep that follows a maintenance (dark) wake. Neither overrides the user's own
sleep or a closed lid without clamshell mode (an external display on AC power): only a root `pmset`
change does, and the harness never makes one. `-w` ends the assertion by itself if the daemon dies,
so nothing is left behind.

While there is work, a closed lid that still sleeps the Mac (no clamshell mode, or on battery) is
recorded too, so status says plainly that work pauses while the Mac sleeps. It clears when the lid opens.

Checked on the daemon's tick; the power source and the lid are read at most every POWER_EVERY_S. On
any other platform nothing is read, spawned or recorded.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from typing import Callable

KV = "idle_sleep"           # what `ttp status` and the web app show: {"daemon", "held", "pid", "why"[, "lid_closed"]}
POWER_EVERY_S = 15.0
MODES = ("auto", "on", "off")
LID_LINE = "the lid is closed: work pauses while the Mac sleeps"
BATTERY = "on battery power"   # IdleHold's why when work wants the hold but the Mac is not on AC power
BATTERY_LINE = f"idle sleep: not held: {BATTERY} (keep-awake holds only on AC power, so work pauses if the Mac sleeps)"
LID_BATTERY_LINE = f"the lid is closed {BATTERY}: work pauses while the Mac sleeps (keep-awake holds only on AC power)"


def mode(cfg: dict) -> str:
    """runner.prevent_idle_sleep as auto/on/off (true/false accepted); unset or unreadable is off. The
    older power.keep_awake set to off still turns it off."""
    v = (cfg.get("runner") or {}).get("prevent_idle_sleep", "off")
    v = {True: "on", False: "off"}.get(v, v) if isinstance(v, bool) else str(v).strip().lower()
    legacy = (cfg.get("power") or {}).get("keep_awake")
    if legacy is False or str(legacy).lower() in ("off", "never", "false"):
        return "off"
    return v if v in MODES else "off"


def enabled(cfg: dict, platform: str | None = None) -> bool:
    """Whether the config allows holding the assertion here: only when set to on or auto (auto: on for
    workstations; a macOS host counts as one), and no other platform is ever asked."""
    if (platform or sys.platform) != "darwin":
        return False
    return mode(cfg) != "off"


def on_ac_power(run: Callable = subprocess.run) -> bool:
    """`pmset -g ps` names 'AC Power' as the source. Anything unreadable counts as battery."""
    try:
        out = run(["pmset", "-g", "ps"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and "'AC Power'" in (out.stdout or "")


def lid(run: Callable = subprocess.run) -> dict | None:
    """The lid as the power manager sees it (`ioreg -r -k AppleClamshellState`, read-only):
    {"closed": bool, "causes_sleep": bool}, or None (no lid, or unreadable). AppleClamshellCausesSleep
    is No in clamshell mode (an external display on AC power); a missing one counts as Yes."""
    try:
        out = run(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "1"], capture_output=True, text=True,
                  timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or "") if out.returncode == 0 else ""
    state = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)', text)
    if not state:
        return None
    causes = re.search(r'"AppleClamshellCausesSleep"\s*=\s*(Yes|No)', text)
    return {"closed": state.group(1) == "Yes", "causes_sleep": not causes or causes.group(1) == "Yes"}


def argv(pid: int) -> list[str]:
    return ["caffeinate", "-i", "-s", "-w", str(pid)]


class IdleHold:
    """The daemon's one sleep assertion. `update` is called every tick; `release` on shutdown."""

    def __init__(self, platform: str | None = None, power: Callable[[], bool] = on_ac_power,
                 spawn: Callable = subprocess.Popen, pid: int | None = None, record: Callable | None = None,
                 read_lid: Callable[[], dict | None] = lid):
        self.platform = platform or sys.platform
        self.power = power
        self.read_lid = read_lid
        self.spawn = spawn
        self.pid = pid or os.getpid()
        self.record = record or (lambda state: None)
        self.proc: subprocess.Popen | None = None
        self._ac: tuple[float, bool] | None = None   # (monotonic time read, on AC)
        self._lid: tuple[float, dict | None] | None = None   # (monotonic time read, lid())
        self._lid_closed = False   # closed and the Mac sleeps anyway; recorded only while there is work
        self._state: dict | None = None
        self._failed = ""

    def update(self, cfg: dict, has_work: Callable[[], bool]) -> None:
        if self.platform != "darwin":
            return
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None   # it ended on its own (killed by someone): started again below if still wanted
        why = ""
        if not enabled(cfg, self.platform):
            why = "off by config (runner.prevent_idle_sleep)"
        work = has_work()
        self._lid_closed = bool(work) and self._lid_sleeps()
        if work and not why and not self._on_ac():
            why = BATTERY
        if not work or why:
            self._stop()
            self._show(held=False, why=why if work else "")
            return
        if self.proc is None:
            try:
                self.proc = self.spawn(argv(self.pid), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
                self._failed = ""
            except OSError as e:
                self._failed = f"caffeinate could not start: {e.strerror or e}"
        if self.proc is None:
            self._show(held=False, why=self._failed)
        else:
            self._show(held=True, why="")

    def release(self) -> None:
        self._stop()
        self._lid_closed = False
        self._show(held=False, why="")

    def _lid_sleeps(self) -> bool:
        """The lid is closed and the Mac sleeps anyway: no clamshell mode, or on battery."""
        now = time.monotonic()
        if self._lid is None or now - self._lid[0] >= POWER_EVERY_S:
            self._lid = (now, self.read_lid())
        state = self._lid[1]
        if not state or not state.get("closed"):
            return False
        return bool(state.get("causes_sleep", True)) or not self._on_ac()

    def _on_ac(self) -> bool:
        now = time.monotonic()
        if self._ac is None or now - self._ac[0] >= POWER_EVERY_S:
            self._ac = (now, bool(self.power()))
        return self._ac[1]

    def _stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        except OSError:
            pass

    def _show(self, held: bool, why: str) -> None:
        state = {"daemon": self.pid, "held": held, "pid": self.proc.pid if held and self.proc else None,
                 "why": why} if held or why or self._lid_closed else None
        if state is not None and self._lid_closed:
            state["lid_closed"] = True
        if state != self._state:
            self._state = state
            self.record(state)


def line(state: dict | None, daemon_pid: int | None) -> str:
    """The status line for what the running daemon recorded, or '' (nothing worth saying, another
    daemon's leftover, or not a Mac). A closed lid that sleeps the Mac says so instead of the hold."""
    if not state or not daemon_pid or state.get("daemon") != daemon_pid:
        return ""
    if state.get("lid_closed"):
        return LID_BATTERY_LINE if state.get("why") == BATTERY else LID_LINE
    if state.get("held"):
        return f"idle sleep: held while there is work (caffeinate pid {state.get('pid')})"
    if state.get("why") == BATTERY:
        return BATTERY_LINE
    return f"idle sleep: not held: {state['why']}" if state.get("why") else ""


def pause_line(text: str) -> bool:
    """`text` (from line()) says that work pauses now: the lid is closed and the Mac sleeps."""
    return text in (LID_LINE, LID_BATTERY_LINE)
