# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Restart a daemon that is alive but no longer completes ticks. The systemd unit's watchdog timer,
the launchd watchdog agent and the crontab entry run this every few minutes; systemd's WatchdogSec is
only the backstop behind it.

One stale look is not enough: a laptop waking from sleep shows an old heartbeat until the daemon's
next tick. The daemon counts as hung only when two looks at least CONFIRM_S apart saw the same
heartbeat and it is older than the hang threshold: config `watchdog.hang_s` (900 s by default, never
below WATCHDOG_S, well above any single tick step; a long tick's steps refresh the heartbeat). A daemon
whose own tick watch reported a stall in a named step after that heartbeat (STALL_FILE, written TICK_STALL_S
after its last progress) is hung at WATCHDOG_S already: twice the stale threshold, so with the timer's
5-minute looks it is restarted 10 to 15 minutes after its last progress. A step that keeps telling the
watchdogs it moves is never restarted (check_overrun alerts it instead).

A hung daemon is then, with no model: sent SIGUSR1, which makes it write every thread's stack
(faulthandler, registered at daemon start) to state/daemon-stacks.log; the new part of that file is
saved as an incident file under state/incidents/. It is restarted through its service (systemd: the
hung pid is killed, then `systemctl --user restart` with a timeout; past it, stop, kill and start;
launchd and cron: the pid is ended and the service starts a new one), and one alert per incident
under the key `daemon-hang` names the project, the step it was stuck in, the dump and whether the restart
worked: low when it did, high when it did not. It clears itself once a daemon completes a tick again.
The new daemon reaps a run whose reap the hang cut off once more without its optional checks, and
quarantines it if that is cut off too (Daemon._reap_mark), so a restart never re-enters the same hang. Each restart within `watchdog.window_s` doubles the threshold for the next one (up to
16 times), and the `watchdog.restarts`-th restart in that window escalates once with a high alert.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .daemon import (HEARTBEAT_STALE_S, STACKS_FILE, STALL_FILE, WATCHDOG_S, _alive, _is_daemon, _read_pid,
                     heartbeat, log, start_marker)
from .project import Project, durable_write

CONFIRM_S = 60
TERM_GRACE_S = 15   # a daemon stuck in a system call may never run its TERM handler
HANG_S = 900        # default watchdog.hang_s
DUMP_WAIT_S = 5     # how long the stack dump may take to arrive after SIGUSR1
RESTART_TIMEOUT_S = 60
STOP_TIMEOUT_S = 30
KEEP_INCIDENTS = 20
HANG_REF = "daemon-hang"          # the alert's condition key (alerts.holds)
HANG_KEY = "daemon_hang"          # kv: the open incident {pid, at, dump, restarted, how}
RESTARTS_KEY = "daemon_hang_restarts"    # kv: when the watchdog restarted a hung daemon
ESCALATED_KEY = "daemon_hang_escalated"  # kv: when it last escalated a repeat hang


def settings(p: Project) -> dict:
    """{hang_s, restarts, window_s} from config `watchdog`, with sane floors."""
    try:
        cfg = p.config().get("watchdog") or {}
    except Exception:   # an unreadable project.json must not stop the watchdog
        cfg = {}

    def num(key: str, default: float, floor: float) -> float:
        try:
            return max(floor, float(cfg.get(key) or default))
        except (TypeError, ValueError):
            return default
    return {"hang_s": num("hang_s", HANG_S, WATCHDOG_S), "restarts": int(num("restarts", 3, 1)),
            "window_s": num("window_s", 21600, 0)}


def threshold(p: Project, cfg: dict, now: float, stalled: bool = False) -> float:
    """The staleness that counts as hung now: hang_s (WATCHDOG_S for a daemon stalled in a named step,
    stalled_step), doubled for each restart within the window."""
    recent = [t for t in p.db.kv(RESTARTS_KEY, []) or [] if now - float(t) < cfg["window_s"]]
    return (min(cfg["hang_s"], WATCHDOG_S) if stalled else cfg["hang_s"]) * 2 ** min(len(recent), 4)


def stalled_step(p: Project, pid: int, since: float) -> str | None:
    """The tick step daemon pid reported itself stalled in (STALL_FILE) after its last progress the
    watchdog saw (`since`), else None. "" when it stalled before its first step."""
    try:
        rec = json.loads((p.state / STALL_FILE).read_text())
        if int(rec.get("pid") or 0) == pid and float(rec.get("at") or 0) >= since:
            return str(rec.get("step") or "")
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def last_tick(p: Project, pid: int) -> float | None:
    """When daemon pid last showed progress: its heartbeat (a completed tick, or a step of a long one),
    else its start or the last step of its first tick."""
    hb = heartbeat(p)
    if hb and int(hb.get("pid") or 0) == pid:
        return time.time() - hb["age"]
    st = start_marker(p)
    if st and int(st.get("pid") or 0) == pid:
        return max(float(st.get("started") or 0), float(st.get("progress") or 0)) or None
    return None


def check(p: Project, now: float | None = None, grace_s: float = TERM_GRACE_S,
          dump_wait_s: float = DUMP_WAIT_S) -> str:
    """Look once; dump, restart and report the daemon if it is hung. Returns what it found."""
    now = time.time() if now is None else now
    mark = p.state / "watchdog.json"
    pid = _read_pid(p.state / "daemon.pid")
    since = last_tick(p, pid) if pid and _is_daemon(pid) else None
    if since is None or now - since <= HEARTBEAT_STALE_S:
        mark.unlink(missing_ok=True)
        if since is not None:
            _clear_if_ticking(p, pid)
        return "ok" if since else "not running"
    try:
        prev = json.loads(mark.read_text())
    except (OSError, ValueError):
        prev = {}
    same = prev.get("pid") == pid and abs(float(prev.get("since") or 0) - since) < 1
    if not same:
        mark.write_text(json.dumps({"pid": pid, "since": since, "seen": now}))
        return "stale"
    cfg = settings(p)
    step = stalled_step(p, pid, since)
    if now - since <= threshold(p, cfg, now, step is not None) or now - float(prev.get("seen") or now) < CONFIRM_S:
        return "stale"
    mark.unlink(missing_ok=True)
    return _recover(p, pid, since, now, cfg, grace_s, dump_wait_s, step)


def _clear_if_ticking(p: Project, pid: int) -> None:
    """End the open incident once a daemon completes a tick after it (alerts.sweep then clears the alert)."""
    rec = p.db.kv(HANG_KEY)
    if not rec:
        return
    hb = heartbeat(p) or {}
    if int(hb.get("pid") or 0) == pid and (pid != rec.get("pid") or float(hb.get("tick_done") or 0) > rec["at"]):
        p.db.set_kv(HANG_KEY, None)


def _recover(p: Project, pid: int, since: float, now: float, cfg: dict, grace_s: float, dump_wait_s: float,
             step: str | None = None) -> str:
    mins = int((now - since) // 60)
    where = f" (stalled in tick step {step or '(before the steps)'})" if step is not None else ""
    log(p, f"watchdog: daemon pid={pid} alive but completed no tick for {mins} min{where}; dumping its stacks "
           f"and restarting it")
    dump = save_dump(p, pid, now, mins, dump_wait_s)
    ok, how = _restart(p, pid, grace_s)
    log(p, f"watchdog: {'restarted' if ok else 'restart failed'}: {how}")
    db = p.db
    restarts = [float(t) for t in db.kv(RESTARTS_KEY, []) or [] if now - float(t) < cfg["window_s"]] + [now]
    db.set_kv(RESTARTS_KEY, restarts)
    db.set_kv(HANG_KEY, {"pid": pid, "at": now, "dump": str(dump), "restarted": ok, "how": how})
    told = (f"Project {p.name}: its daemon (pid {pid}) was alive but completed no tick for {mins} min{where}. "
            f"Its stacks are in {dump}. ")
    told += (f"The watchdog restarted it ({how}); running workers were kept." if ok else
             f"The watchdog could not restart it: {how}.")
    db.post("out", told, kind="alert", severity="low" if ok else "high", ref=HANG_REF)
    last = float(db.kv(ESCALATED_KEY, 0) or 0)
    if len(restarts) >= cfg["restarts"] and now - last >= cfg["window_s"]:
        db.set_kv(ESCALATED_KEY, now)
        hours = cfg["window_s"] / 3600
        db.post("out", f"Project {p.name}: its daemon hung {len(restarts)} times in {hours:g} h; each was "
                       f"restarted, and the next restart waits {int(threshold(p, cfg, now) // 60)} min of no tick. "
                       f"The stack dumps are in {dump.parent}.", kind="alert", severity="high", ref=HANG_REF)
    return "restarted" if ok else "restart failed"


def save_dump(p: Project, pid: int, now: float, mins: int, wait_s: float) -> Path:
    """Ask the daemon for its stacks (SIGUSR1) and save what it wrote as an incident file."""
    stacks = p.state / STACKS_FILE
    before = stacks.stat().st_size if stacks.exists() else 0
    st = start_marker(p) or {}
    # SIGUSR1's default action ends a process: only a daemon that registered the dump gets it.
    note = ""
    if int(st.get("pid") or 0) != pid or not st.get("stacks"):
        note = "This daemon did not register a stack dump (an older runtime): no stacks."
    else:
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError as e:
            note = f"SIGUSR1 failed: {e}"
    size, deadline = before, time.time() + (0 if note else wait_s)
    while time.time() < deadline:
        time.sleep(0.2)
        cur = stacks.stat().st_size if stacks.exists() else 0
        if cur > before and cur == size:   # it arrived and stopped growing
            break
        size = cur
    try:
        with open(stacks, "rb") as f:
            f.seek(before)
            text = f.read(1 << 20).decode(errors="replace")
    except OSError:
        text = ""
    if not text and not note:
        note = f"No stack dump arrived within {wait_s:g} s of SIGUSR1."
    inc = p.state / "incidents"
    inc.mkdir(parents=True, exist_ok=True)
    path = inc / f"daemon-hang-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}-{pid}.txt"
    durable_write(path, f"daemon pid {pid} of project {p.name}: alive, no completed tick for {mins} min\n"
                        f"{note}\n{text}")
    for old in sorted(inc.glob("daemon-hang-*.txt"))[:-KEEP_INCIDENTS]:
        old.unlink(missing_ok=True)
    return path


def _systemctl(*args: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)


def _ctl(*args: str, timeout: float) -> str:
    """Run one systemctl step; what it did, in a few words."""
    try:
        r = _systemctl(*args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout:g} s"
    except OSError as e:
        return f"could not run: {e}"
    return "ok" if r.returncode == 0 else f"failed: {(r.stderr or r.stdout or '').strip()[:200]}"


def _restart(p: Project, pid: int, grace_s: float) -> tuple[bool, str]:
    """Restart the hung daemon through its service. Returns (it worked, how)."""
    from . import service
    if why := service.foreign_refusal(p, "restart"):
        return False, why   # from a run of another project: left as it is
    svc = service.installed(p)
    if svc and svc["kind"] == "systemd":
        unit = service._unit(p).name
        # Its SIGTERM handler only asks it to stop after the tick, which a hung daemon never ends, so a
        # restart would wait out the unit's stop timeout (90 s, past RESTART_TIMEOUT_S). The stacks
        # are saved: kill the hung pid first, then the restart only starts a new daemon.
        killed = _end(pid, 0)
        did = _ctl("restart", unit, timeout=RESTART_TIMEOUT_S)
        if did == "ok" and not _alive(pid):
            return True, (f"killed hung pid {pid}, " if killed else "") + f"systemctl --user restart {unit}"
        # Escalate: stop the unit, kill the pid if the stop did not end it, start it again.
        why = f"systemctl --user restart {unit} {did}" if did != "ok" else f"pid {pid} survived the restart"
        stop = _ctl("stop", unit, timeout=STOP_TIMEOUT_S)
        killed = _end(pid, 0)
        start = _ctl("start", unit, timeout=RESTART_TIMEOUT_S)
        how = (f"{why}; escalated: stop {stop}, " + ("killed pid " if killed else "pid survived SIGKILL ")
               + f"{pid}, start {start}")
        return start == "ok" and not _alive(pid), how
    if not _end(pid, grace_s):
        return False, f"pid {pid} survived SIGTERM and SIGKILL"
    if svc and svc["kind"] == "launchd":
        # KeepAlive starts a new one at once, but only while its agent is loaded: a bootout whose
        # bootstrap failed leaves it unloaded, and then nothing would.
        loaded = service.ensure_loaded(p)
        if loaded and not loaded.startswith("bootstrapped"):
            return False, f"ended pid {pid}; its launchd agent is not loaded: {loaded}"
        return True, f"ended pid {pid}; " + (f"{loaded}, which starts a new daemon" if loaded
                                              else "its launchd service starts a new daemon")
    if svc:   # cron starts a new one right after this
        return True, f"ended pid {pid}; its {svc['kind']} service starts a new daemon"
    service._spawn(p)   # a daemon started by hand: started again the way cron does
    return True, f"ended pid {pid} and started a new daemon (no service installed)"


def _end(pid: int, grace_s: float) -> bool:
    """SIGTERM, then SIGKILL; whether pid is gone."""
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 5)):
        if wait <= 0:
            continue
        try:
            os.kill(pid, sig)
        except OSError:
            return not _alive(pid)
        deadline = time.time() + wait
        while time.time() < deadline and _alive(pid):
            time.sleep(0.5)
        if not _alive(pid):
            return True
    return not _alive(pid)


def main() -> int:
    began = time.time()
    p = Project(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    if p.state.is_dir():
        found = check(p)
        if found not in ("ok", "not running"):   # it runs every few minutes: log only what matters
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} watchdog: {found}")
        if found == "not running":
            try:   # no daemon: KeepAlive starts one only while its launchd agent is loaded
                from . import service
                loaded = service.ensure_loaded(p)
            except Exception as e:  # never in the way of the hang check
                loaded = f"error: {type(e).__name__}: {e}"
            if loaded:
                print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} watchdog: no daemon running; {loaded}")
        try:   # a restart deferred past the old daemon's stop timeout: judge its new daemon now
            from . import service
            judged = service.check_deferred(p, budget_s=service.WATCHDOG_RUN_S - (time.time() - began))
        except Exception as e:  # never in the way of the hang check
            judged = f"error: {type(e).__name__}: {e}"
        if judged not in (None, "pending"):
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} watchdog: deferred restart {judged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
