"""Keep a Mac from idle-sleeping while its project has work.

On macOS only, the daemon holds an idle-sleep assertion (`caffeinate -i -w <daemon pid>`) while all
of these hold: the project has work (a running run or task, or queued work that can start now), the
machine is on AC power, and `runner.prevent_idle_sleep` allows it. It lets go as soon as one stops
holding, and on shutdown. `-i` blocks idle sleep only: the user's own sleep, a closed lid and display
sleep are untouched. `-w` ends the assertion by itself if the daemon dies, so nothing is left behind.

Checked on the daemon's tick; the power source is read at most every POWER_EVERY_S. On any other
platform nothing is read, spawned or recorded.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Callable

KV = "idle_sleep"           # what `ttp status` and the web app show: {"daemon", "held", "pid", "why"}
POWER_EVERY_S = 15.0
MODES = ("auto", "on", "off")


def mode(cfg: dict) -> str:
    """runner.prevent_idle_sleep as auto/on/off (true/false accepted). The older power.keep_awake set
    to off still turns it off."""
    v = (cfg.get("runner") or {}).get("prevent_idle_sleep", "auto")
    v = {True: "on", False: "off"}.get(v, v) if isinstance(v, bool) else str(v).strip().lower()
    legacy = (cfg.get("power") or {}).get("keep_awake")
    if legacy is False or str(legacy).lower() in ("off", "never", "false"):
        return "off"
    return v if v in MODES else "auto"


def enabled(cfg: dict, platform: str | None = None) -> bool:
    """Whether the config allows holding the assertion here. auto: on for workstations; a macOS host
    counts as one, and no other platform is ever asked."""
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


def argv(pid: int) -> list[str]:
    return ["caffeinate", "-i", "-w", str(pid)]


class IdleHold:
    """The daemon's one idle-sleep assertion. `update` is called every tick; `release` on shutdown."""

    def __init__(self, platform: str | None = None, power: Callable[[], bool] = on_ac_power,
                 spawn: Callable = subprocess.Popen, pid: int | None = None, record: Callable | None = None):
        self.platform = platform or sys.platform
        self.power = power
        self.spawn = spawn
        self.pid = pid or os.getpid()
        self.record = record or (lambda state: None)
        self.proc: subprocess.Popen | None = None
        self._ac: tuple[float, bool] | None = None   # (monotonic time read, on AC)
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
        if work and not why and not self._on_ac():
            why = "on battery power"
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
        self._show(held=False, why="")

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
                 "why": why} if held or why else None
        if state != self._state:
            self._state = state
            self.record(state)


def line(state: dict | None, daemon_pid: int | None) -> str:
    """The status line for what the running daemon recorded, or '' (nothing worth saying, another
    daemon's leftover, or not a Mac)."""
    if not state or not daemon_pid or state.get("daemon") != daemon_pid:
        return ""
    if state.get("held"):
        return f"idle sleep: held while there is work (caffeinate pid {state.get('pid')})"
    return f"idle sleep: not held: {state['why']}" if state.get("why") else ""
