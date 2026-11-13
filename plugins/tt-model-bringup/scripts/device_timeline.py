#!/usr/bin/env python3
"""Device held/idle timeline of a stage, written by the multigoal runner (no model, no tokens).

A background thread of the runner looks every 15 s at ``/proc/*/fd`` for links to
``/dev/tenstorrent/*``. It never opens a device and never calls tt-smi. Only changes are
logged, one JSON line each, to ``<stage doc dir>/device_timeline.jsonl``::

    {"t": <unix s>, "held": <devices held | null>, "of": <devices present>,
     "by": "<cmdline of first holder>", "other": <true if no holder descends from the runner>}

plus a line (with an ``event`` field) at stage start, at each record write / stage end and at
runner (re)start. ``held`` is null (unknown, not free) when fds could not be read and no holder
was seen; ``unreadable`` (optional) counts the processes whose fds could not be read. A
``runner_start`` line carries ``prev_alive``, the last tick of the previous runner (a one-number
file next to the timeline), so the time the runner was down counts as unknown.

Held means held, not busy: an idle server counts as held. Memory is constant: the summary is
computed by streaming the file.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import threading
import time
from typing import Any

INTERVAL = 15
DEVICE_PREFIX = "/dev/tenstorrent/"
TIMELINE = "device_timeline.jsonl"
ALIVE = "device_timeline.alive"
BY_CHARS = 120


def _ppid(proc: pathlib.Path, pid: int) -> int | None:
    try:
        stat = (proc / str(pid) / "stat").read_text(errors="replace")
        return int(stat[stat.rindex(")") + 2 :].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def descends_from(proc: pathlib.Path, pid: int, root: int) -> bool:
    """True when ``pid`` is ``root`` or one of its descendants (walks the parent chain)."""
    seen: set[int] = set()
    while pid and pid not in seen:
        if pid == root:
            return True
        seen.add(pid)
        parent = _ppid(proc, pid)
        if parent is None:
            return False
        pid = parent
    return False


def scan(proc: pathlib.Path, dev_dir: pathlib.Path, root_pid: int) -> dict[str, Any]:
    """One look at the host: devices present, devices held, first holder, other, unreadable."""
    try:
        present = sum(1 for _ in os.scandir(dev_dir))
    except OSError:
        present = 0
    held: set[str] = set()
    holders: list[int] = []
    unreadable = 0
    try:
        pids = sorted(int(entry) for entry in os.listdir(proc) if entry.isdigit())
    except OSError:
        return {"held": None, "of": present, "by": "", "other": False, "unreadable": 1}
    for pid in pids:
        fd_dir = proc / str(pid) / "fd"
        try:
            names = os.listdir(fd_dir)
        except (PermissionError, OSError) as exc:
            if not isinstance(exc, FileNotFoundError):
                unreadable += 1
            continue
        mine = False
        for name in names:
            try:
                target = os.readlink(fd_dir / name)
            except FileNotFoundError:
                continue
            except OSError:
                unreadable += 1
                break
            if target.startswith(DEVICE_PREFIX):
                held.add(target)
                mine = True
        if mine:
            holders.append(pid)
    result: dict[str, Any] = {"held": len(held), "of": present, "by": "", "other": False}
    if holders:
        try:
            raw = (proc / str(holders[0]) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            raw = ""
        result["by"] = (raw.strip() or f"pid {holders[0]}")[:BY_CHARS]
        result["other"] = not any(descends_from(proc, pid, root_pid) for pid in holders)
    elif unreadable:
        result["held"] = None  # something we could not read may hold a device: unknown, not free
    if unreadable:
        result["unreadable"] = unreadable
    return result


class DeviceTimeline:
    """Background check for one stage. ``stage_start`` or ``runner_start`` is written on start."""

    def __init__(
        self,
        path: pathlib.Path,
        *,
        fresh: bool,
        root_pid: int | None = None,
        proc: pathlib.Path = pathlib.Path("/proc"),
        dev_dir: pathlib.Path = pathlib.Path("/dev/tenstorrent"),
        interval: float = INTERVAL,
        clock=time.time,
    ) -> None:
        self.path = pathlib.Path(path)
        self.alive = self.path.with_name(ALIVE)
        self.fresh = fresh
        self.root_pid = root_pid or os.getpid()
        self.proc, self.dev_dir, self.interval, self.clock = proc, dev_dir, interval, clock
        self.last: tuple | None = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def _append(self, line: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")

    def _prev_alive(self) -> int | None:
        try:
            return int(float(self.alive.read_text().strip()))
        except (OSError, ValueError):
            return None

    def tick(self, event: str | None = None, **extra: Any) -> None:
        """Look once. Log when the state changed or when an event line is due."""
        with self.lock:
            now = int(self.clock())
            seen = scan(self.proc, self.dev_dir, self.root_pid)
            state = (seen["held"], seen["of"], seen["by"], seen["other"], seen.get("unreadable", 0))
            if event or state != self.last:
                line: dict[str, Any] = {"t": now, **{k: seen[k] for k in ("held", "of", "by", "other")}}
                if seen.get("unreadable"):
                    line["unreadable"] = seen["unreadable"]
                if event:
                    line["event"] = event
                line.update(extra)
                self._append(line)
            self.last = state
            try:
                self.alive.write_text(f"{now}\n")
            except OSError:
                pass

    def start(self) -> None:
        if self.fresh:
            self.tick("stage_start", host=socket.gethostname(), uid=os.getuid(), runner_pid=self.root_pid,
                      pidns=_pidns(self.proc), interval=self.interval)
        else:
            self.tick("runner_start", prev_alive=self._prev_alive(), runner_pid=self.root_pid,
                      pidns=_pidns(self.proc), host=socket.gethostname(), uid=os.getuid())
        self.thread = threading.Thread(target=self._loop, name="device-timeline", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a failed look is a gap, which reads as unknown
                pass

    def mark_end(self) -> int:
        """A stage-end line (stage end as of this record). Returns its time."""
        self.tick("stage_end")
        return int(self.clock())

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.mark_end()


def _pidns(proc: pathlib.Path) -> str:
    try:
        return os.readlink(proc / "self" / "ns" / "pid")
    except OSError:
        return ""


def summarize(path: pathlib.Path, end: int | None = None) -> dict[str, Any] | None:
    """Exact seconds held / held by other / idle / unknown since the last ``stage_start`` line.

    Streams the file. Returns None when there is no timeline or no line. Seconds from the
    first line (stage start) to ``end`` (default: the last line) are partitioned exactly:
    held + held_other + idle + unknown == wall.
    """
    try:
        handle = pathlib.Path(path).open(encoding="utf-8")
    except OSError:
        return None
    totals = {"held_s": 0, "held_other_s": 0, "idle_s": 0, "unknown_s": 0}
    where: dict[str, Any] = {}
    prev: dict[str, Any] | None = None
    first_t = 0
    complete = True
    devices = 0

    def add(state: dict[str, Any], seconds: int) -> None:
        nonlocal complete
        if seconds <= 0:
            return
        if state.get("held") is None:
            totals["unknown_s"] += seconds
            complete = False
        elif state["held"] > 0:
            totals["held_other_s" if state.get("other") else "held_s"] += seconds
        else:
            totals["idle_s"] += seconds
        if state.get("unreadable"):
            complete = False

    with handle:
        for raw in handle:
            try:
                line = json.loads(raw)
                t = int(line["t"])
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(line, dict):
                continue
            if line.get("event") == "stage_start" or prev is None:
                totals = {key: 0 for key in totals}
                complete, devices, prev, first_t = True, 0, None, t
                where = {key: line[key] for key in ("host", "uid", "pidns") if key in line}
                if line.get("event") != "stage_start":
                    complete = False  # the start of the stage was not seen
            if prev is not None:
                if line.get("event") == "runner_start":
                    # The runner was down: from its last tick to this start is unknown.
                    alive = line.get("prev_alive")
                    alive = prev["t"] if not isinstance(alive, int) else min(max(alive, prev["t"]), t)
                    add(prev, alive - prev["t"])
                    if t - alive > 2 * where_interval(where, line):
                        totals["unknown_s"] += t - alive
                        complete = False
                    else:
                        add(prev, t - alive)
                else:
                    add(prev, t - prev["t"])
            devices = max(devices, int(line.get("of") or 0))
            prev = {"t": t, "held": line.get("held"), "other": bool(line.get("other")),
                    "unreadable": line.get("unreadable", 0)}
            if line.get("interval"):
                where["interval"] = line["interval"]
    if prev is None:
        return None
    stop = prev["t"] if end is None else max(end, prev["t"])
    add(prev, stop - prev["t"])
    return {**totals, "wall_s": stop - first_t, "devices": devices, "complete": complete,
            "timeline": TIMELINE, **{k: v for k, v in where.items() if k != "interval"}}


def where_interval(where: dict[str, Any], line: dict[str, Any]) -> float:
    return float(where.get("interval") or INTERVAL)
