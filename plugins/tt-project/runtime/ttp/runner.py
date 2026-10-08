# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Detached run supervisor. The daemon writes <run_dir>/run.json and starts
`python -m ttp._run <run_dir>` in its own session; this process owns the agent child:

- feeds the prompt on stdin (no argv size limit), streams output to output.jsonl;
- refreshes <run_dir>/lease every 30 s while the child lives, so liveness never depends on a model
  remembering to report it;
- enforces the wall-clock limit with TERM, then KILL 30 s later (a child that ignores TERM would
  otherwise run on past its bound). The limit and the stall guard count awake time (AwakeClock):
  monotonic time, which stands still while some hosts sleep, less any gap between two polls longer
  than SLEEP_GAP_S, so a host whose monotonic clock runs through a suspend does not time a run out
  either. exit.json records how long the host slept during the run (`slept_s`);
- ends a run whose agent has produced no progress (assistant message, tool result, streamed
  thinking or progress note) for stall_s, or longer while a tool call that set its own timeout is
  pending: the tool_progress heartbeats and retry notices Claude Code writes while a call hangs on a
  network stall are not progress;
- enforces the run's dollar budget mid-flight when the provider streams usage;
- ends the child when <run_dir>/STOP appears (a cancel, or `ttp stop --kill` writing "shutdown") or
  the run dir is deleted, and never starts it when the STOP came first;
- ends the child when the runner itself dies (killed outright): on Linux the kernel sends it SIGTERM
  (PR_SET_PDEATHSIG), elsewhere a watchdog process ends its process group;
- starts the child `nice` levels below itself (run.json; workers and reviewers only), so all it
  starts runs niced too, and records the level the child ran at in exit.json (`nice`);
- holds a slot of each resource an `exclusive:` task names from before the child starts until it
  has ended, the same locks `ttp lock` takes per command; the wait for them has its own bound
  (exclusive_wait_s), and the wall-clock limit starts once they are held. While a job the agent
  detached still runs, a keeper holds them on until it ends (hold.py);
- extends the wall-clock limit by the time the agent's `ttp lock` commands spent waiting, so work
  queued behind a shared device is not cut off for the queue; at most by the limit itself, since a
  wait in the background (or with --timeout 0) must not lift the only spend bound of providers
  that report cost only at the end;
- writes exit.json exactly once, then asks the provider adapter for usage and records it.

It survives a daemon restart: the daemon re-adopts runs by run_dir, pid and boot id.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import poll_s
from .project import durable_write, lower_priority

LEASE_EVERY_S = 30
KILL_AFTER_S = 30
POLL_S = 5
BUDGET_EVERY_S = 10
TOOL_GRACE_S = 60     # past a pending tool call's own timeout before its silence counts as a stall
SLEEP_GAP_S = 600     # polls are POLL_S apart: a gap this long between two can only be a host sleep
PDEATHSIG = sys.platform.startswith("linux")   # else (macOS) a watchdog ends the agent if the runner dies
PR_SET_PDEATHSIG = 1

# Started with the agent where PDEATHSIG is off: waits on a pipe the runner holds open. A word on it
# is a normal end; end of file without one means the runner died, and the agent's group is ended.
_WATCHDOG = """
import os, signal, sys, time
pgid, grace = int(sys.argv[1]), float(sys.argv[2])
if sys.stdin.buffer.read():
    sys.exit(0)
for sig in (signal.SIGTERM, signal.SIGKILL):
    try:
        os.killpg(pgid, sig)
    except OSError:
        sys.exit(0)
    end = time.time() + grace
    while time.time() < end:
        time.sleep(0.5)
        try:
            os.killpg(pgid, 0)
        except OSError:
            sys.exit(0)
"""


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        pass
    try:  # macOS: whole seconds of kern.boottime; the microseconds field drifts between reads
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
        return out.split("sec =")[1].split(",")[0].strip()
    except Exception:
        return "unknown"


def boot_time() -> float | None:
    """When this host booted, as a Unix time; None if it cannot be told."""
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
        return float(out.split("sec =")[1].split(",")[0].strip())
    except Exception:
        return None


def proc_start(pid: int) -> str | None:
    """When process pid started, as a token that stays the same for its whole life and differs for
    a later process given the same pid. None if it is gone."""
    try:   # Linux: clock ticks from boot to its start (field 22)
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def _touch(p: Path) -> None:
    p.touch()
    os.utime(p, None)


def request_stop(run_dir: Path, why: str = "cancel") -> None:
    """Ask a run's supervisor to end its agent (TERM, then KILL after the grace period)."""
    (run_dir / "STOP").write_text(why)


def stop_runs(db, runs_dir: Path, task: int | None = None, why: str = "cancel") -> list[int]:
    """Request a stop for every running run (of one task, if given). Returns their run ids."""
    sql, args = "SELECT id, dir FROM runs WHERE status='running'", ()
    if task is not None:
        sql, args = sql + " AND task=?", (task,)
    ids = []
    for r in db.q(sql, args):
        run_dir = Path(r["dir"]) if r["dir"] else runs_dir / str(r["id"])
        if run_dir.is_dir():
            request_stop(run_dir, why)
            ids.append(r["id"])
    return ids


def stop_reason(run_dir: Path) -> str | None:
    """"shutdown" when the whole project was stopped (the task goes back to the queue), else
    "stopped" (a cancel). None while there is no STOP file."""
    try:
        text = (run_dir / "STOP").read_text().strip()
    except OSError:
        # The run dir itself is gone (deleted under a live run, e.g. a test's temp dir): nobody can
        # read its output or hand-off any more, so the agent must not run on unattended.
        return None if run_dir.is_dir() else "stopped"
    return "shutdown" if text == "shutdown" else "stopped"


def _write_exit(run_dir: Path, info: dict) -> None:
    """Record how the run ended; never creates a run dir that was deleted (durable_write would)."""
    if run_dir.is_dir():
        try:
            durable_write(run_dir / "exit.json", json.dumps(info))
        except FileNotFoundError:
            pass


def _agent_preexec(nice: int):
    """The agent's preexec_fn and whether it arms PR_SET_PDEATHSIG: its nice level, and on Linux a
    SIGTERM from the kernel the moment the runner dies. The signal follows the thread that started
    the child, here the main thread, which lives as long as the runner."""
    lower = lower_priority(nice)
    if not PDEATHSIG:
        return lower, False
    try:
        import ctypes
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return lower, False
    parent = os.getpid()

    def apply() -> None:
        if lower:
            lower()
        prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        if os.getppid() != parent:   # the runner died before the signal was armed
            os._exit(1)
    return apply, True


def _start_watchdog(pgid: int):
    """Where PDEATHSIG is not armed: a process of its own that ends the agent's process group when
    the runner dies. Returns the pipe's write end and the process (None, None if it cannot start)."""
    r, w = os.pipe()
    try:
        proc = subprocess.Popen([sys.executable, "-c", _WATCHDOG, str(pgid), str(KILL_AFTER_S)], stdin=r,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        os.close(w)
        return None, None
    finally:
        os.close(r)
    return w, proc


def remove_files(paths: list[str]) -> None:
    for f in paths:
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass


def remove_tmp(run_dir: Path, spec: dict) -> None:
    """Delete the run's private temp dir: run dirs are kept, so its scratch must not pile up there.
    Only a dir directly inside the run dir is ever removed."""
    tmp = spec.get("tmp_dir")
    if tmp and Path(tmp).parent.resolve() == run_dir.resolve() and not Path(tmp).is_symlink():
        shutil.rmtree(tmp, ignore_errors=True)


def remove_private(run_dir: Path) -> None:
    """Delete the run's private files (a per-run MCP config may carry credentials) and temp dir."""
    try:
        spec = json.loads((run_dir / "run.json").read_text())
        remove_files(spec.get("private_files") or [])
        remove_tmp(run_dir, spec)
    except (OSError, ValueError, AttributeError):
        pass


def supervise(run_dir: Path) -> int:
    try:
        spec = json.loads((run_dir / "run.json").read_text())
        if not isinstance(spec, dict) or "argv" not in spec or "cwd" not in spec:
            raise ValueError("no argv or cwd")
    except (OSError, ValueError) as e:
        # Empty or cut short (a power cut while it was written): nothing to run. The daemon books a
        # failed run that never launched, so its task is retried.
        now = time.time()
        _write_exit(run_dir, {"rc": None, "started": now, "ended": now, "launched": False,
                              "error": f"run.json unreadable: {e}"[:300]})
        return 1
    argv, env_extra, cwd = spec["argv"], spec.get("env", {}), spec["cwd"]
    timeout_s, budget = float(spec.get("timeout_s", 3600)), spec.get("budget_usd")
    stall_s = float(spec.get("stall_s") or 0)
    env = {**os.environ, **env_extra}
    lease, out_path = run_dir / "lease", run_dir / "output.jsonl"
    started = time.time()
    # A stop that came before the agent started (a cancel right after start_run): it never starts.
    held = None if stop_reason(run_dir) else []
    inherited: list[str] = []
    if held is not None:
        _touch(lease)
        wait_s = min(float(spec.get("exclusive_wait_s") or 600), timeout_s)
        held = _take_exclusive(run_dir, spec.get("exclusive") or [], spec.get("env", {}), started + wait_s,
                               spec.get("holds"), inherited)
    if held is not None and stop_reason(run_dir):
        if held:
            # The slots may have been handed over from an earlier run's keeper: its jobs keep them.
            try:
                from . import hold
                hold.keep(run_dir, spec, held, spec.get("exclusive") or [], inherited)
            except Exception as e:
                print(f"runner: could not keep the run's resources for its detached jobs: {e}", flush=True)
        for h in held:
            h.close()
        held = None
    if held is None:
        remove_files(spec.get("private_files") or [])
        remove_tmp(run_dir, spec)
        _write_exit(run_dir, {"rc": None, "started": started, "ended": time.time(),
                              "stopped": stop_reason(run_dir) or "resource_busy", "launched": False})
        return 1
    started, mono_start = time.time(), time.monotonic()
    awake = AwakeClock()
    prompt = open(run_dir / (spec.get("stdin") or "prompt.md"), "rb")
    out = open(out_path, "wb")
    err = open(run_dir / "stderr.log", "wb")
    nice = int(spec.get("nice") or 0)
    preexec, armed = _agent_preexec(nice)
    child = subprocess.Popen(argv, stdin=prompt, stdout=out, stderr=err, cwd=cwd, env=env,
                             start_new_session=True, preexec_fn=preexec)
    guard, watchdog = (None, None) if armed else _start_watchdog(child.pid)
    (run_dir / "child.pid").write_text(f"{child.pid}\n{proc_start(child.pid) or ''}\n")
    niceness = _niceness(child.pid)
    if nice and niceness is not None and niceness < min(os.nice(0) + nice, 19):
        print(f"runner: could not lower the agent's priority by {nice} (it runs at nice {niceness})", flush=True)
    reason: list[str] = []

    def stop(why: str) -> None:
        if reason:
            return
        reason.append(why)
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.time() + KILL_AFTER_S
        while time.time() < deadline and child.poll() is None:
            time.sleep(1)
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def watch() -> None:
        from . import locks
        from .providers import get_provider  # local import: keeps startup cheap
        prov = get_provider(spec["provider"]).use(spec.get("model", ""), spec.get("prices"))
        last_lease = last_budget = 0.0
        progress, active = ProgressWatch(out_path, run_dir), 0.0
        while child.poll() is None:
            now, up = time.time(), awake.tick()
            if now - last_lease >= LEASE_EVERY_S:
                try:
                    _touch(lease)
                except OSError:   # the run dir is gone: stop_reason below ends the agent
                    pass
                last_lease = now
            if up > timeout_s + min(locks.waited(run_dir), timeout_s):
                threading.Thread(target=stop, args=("timeout",), daemon=True).start()
            if stall_s:
                # Stalled = no assistant message, tool result or progress note for stall_s of awake
                # time, or for a pending tool call's own timeout (plus grace) when that is longer.
                if progress.poll():
                    active = up
                elif up - active > progress.limit(stall_s):
                    threading.Thread(target=stop, args=("stalled",), daemon=True).start()
            if budget is not None and now - last_budget >= BUDGET_EVERY_S:
                last_budget = now
                try:
                    so_far = prov.cost_so_far(out_path)
                except Exception:
                    so_far = None
                if so_far is not None and so_far > float(budget):
                    threading.Thread(target=stop, args=("budget",), daemon=True).start()
            why = stop_reason(run_dir)
            if why:
                threading.Thread(target=stop, args=(why,), daemon=True).start()
            time.sleep(poll_s(POLL_S))

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    rc = child.wait()
    ended, mono_end, up = time.time(), time.monotonic(), awake.tick()
    if guard is not None:   # a normal end: the watchdog leaves the group alone
        try:
            os.write(guard, b"done")
        except OSError:
            pass
        os.close(guard)
        try:
            watchdog.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    remove_files(spec.get("private_files") or [])
    remove_tmp(run_dir, spec)
    if held:
        # A job the agent detached may still use the resources: a keeper takes the open slots over.
        try:
            from . import hold
            hold.keep(run_dir, spec, held, spec.get("exclusive") or [], inherited)
        except Exception as e:
            print(f"runner: could not keep the run's resources for its detached jobs: {e}", flush=True)
    for f in (prompt, out, err, *held):
        f.close()
    exit_info = {"rc": rc, "started": started, "ended": ended, "stopped": reason[0] if reason else None,
                 "slept_s": round(max((ended - started) - min(mono_end - mono_start, up), 0.0), 1), "nice": niceness}
    _write_exit(run_dir, exit_info)
    return rc


def _niceness(pid: int) -> int | None:
    """The nice level process `pid` runs at, or None when it cannot be read (it ended, say)."""
    try:
        return os.getpriority(os.PRIO_PROCESS, pid)
    except OSError:
        return None


# Stream events written while nothing moves: Claude Code's heartbeats for a running tool call and
# rate limit notices. Of system events only streamed thinking is model output (not retries, hooks).
NOISE_TYPES = ("tool_progress", "rate_limit_event", "keep_alive")
SYSTEM_PROGRESS = ("thinking_tokens",)


class AwakeClock:
    """Seconds the host was awake since the clock was made. Each step between two ticks counts the
    smaller of its monotonic and wall-clock advance (a wall clock set back or forward does not
    count), and nothing when it is longer than gap_s: the ticks come every few seconds, so a gap
    that long is the host asleep, also where the monotonic clock keeps running through a suspend."""

    def __init__(self, gap_s: float = SLEEP_GAP_S):
        self.gap_s, self.awake = gap_s, 0.0
        self._mono, self._wall = time.monotonic(), time.time()
        self._lock = threading.Lock()

    def tick(self) -> float:
        with self._lock:
            mono, wall = time.monotonic(), time.time()
            step = max(min(mono - self._mono, wall - self._wall), 0.0)
            if step < self.gap_s:
                self.awake += step
            self._mono, self._wall = mono, wall
            return self.awake


class ProgressWatch:
    """Reads the agent's stream as it grows and tells whether it made progress since the last look:
    an assistant message, a tool result, streamed thinking or a new progress note. Lines that are not
    JSON, and event types it does not know (other providers), count as progress. It also tracks the
    tool calls still waiting for their result and the timeouts they set themselves."""

    def __init__(self, out_path: Path, run_dir: Path):
        self.out_path, self.note_path = out_path, run_dir / "progress.md"
        self.offset, self.partial = 0, b""
        self.note = self._note_mark()
        self.pending: dict[str, float] = {}   # tool_use id -> its own timeout in seconds (0 = none)

    def _note_mark(self):
        try:
            st = self.note_path.stat()
            return (st.st_size, st.st_mtime)
        except OSError:
            return None

    def poll(self) -> bool:
        moved = False
        note = self._note_mark()
        if note != self.note:
            self.note, moved = note, True
        try:
            with open(self.out_path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return moved
        self.offset += len(chunk)
        lines = (self.partial + chunk).split(b"\n")
        self.partial = lines.pop()
        for line in lines:
            if line.strip() and self._event(line):
                moved = True
        return moved

    def limit(self, stall_s: float) -> float:
        """How long silence may last now: stall_s, or a pending call's own timeout plus grace."""
        longest = max(self.pending.values(), default=0.0)
        return max(stall_s, longest + TOOL_GRACE_S) if longest else stall_s

    def _event(self, line: bytes) -> bool:
        try:
            e = json.loads(line)
        except ValueError:
            return True
        if not isinstance(e, dict):
            return True
        kind = e.get("type")
        if kind in NOISE_TYPES:
            return False
        if kind == "system":
            return e.get("subtype") in SYSTEM_PROGRESS
        msg = e.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if kind == "assistant" and block.get("type") == "tool_use" and block.get("id"):
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                try:
                    own = float(inp.get("timeout") or 0) / 1000.0   # Claude Code tools take ms
                except (TypeError, ValueError):
                    own = 0.0
                self.pending[block["id"]] = own
            elif block.get("type") == "tool_result":
                self.pending.pop(block.get("tool_use_id"), None)
        return True


def _take_exclusive(run_dir: Path, wanted: list[dict], env: dict, deadline: float, holds: str | None = None,
                    inherited: list | None = None) -> list | None:
    """One slot of each resource, waiting while `ttp lock` commands hold them all. The daemon starts
    an exclusive task only when a slot is free, so a wait here is a race it lost; the resource stays
    reserved while it waits, so new `ttp lock` commands let it in. A slot an earlier run of the same
    task keeps for its detached jobs (hold.py) is handed over, and those jobs go to `inherited`. The
    wait ends at the deadline or on a stop; None when it ended without the slots."""
    from . import hold, locks
    task = f"task #{env.get('TTP_TASK') or '?'}"
    who = f"{task} (run {env.get('TTP_RUN_ID') or '?'}), whole run"
    held, told, asked = [], 0.0, set()
    for res in wanted:
        paths = [Path(x) for x in res["paths"]]
        mark = Path(res["reserve"]) if res.get("reserve") else None
        # A shared resource's holder names the project as well (shared.holder).
        mine = res.get("holder") or task
        while True:
            # Not in the `ttp lock` arrival queue: the reservation below already holds new commands
            # off, and a queued command that is waiting on the reservation must not hold this off.
            f = locks.try_take(paths, who if mine == task else f"{mine}{who[len(task):]}", "exclusive")
            if f:
                held.append(f)
                if mark:
                    locks.unreserve(mark, mine)
                break
            if inherited is not None and res["resource"] not in asked:
                asked.add(res["resource"])
                inherited += [j for j in hold.take_over(holds, mine, res["resource"], str(env.get("TTP_RUN_ID") or "?"))
                              if j not in inherited]
            if stop_reason(run_dir) or time.time() > deadline:
                for h in held:
                    h.close()
                if mark:
                    locks.unreserve(mark, mine)
                return None
            if mark:
                locks.reserve(mark, mine)
            _touch(run_dir / "lease")
            if time.time() - told >= 120:
                with open(run_dir / "progress.md", "a") as pf:
                    pf.write(f"{time.strftime('%H:%M:%S')} waiting for {res['resource']} "
                             f"(held by {', '.join(locks.holders(paths)) or 'another task'})\n")
                told = time.time()
            time.sleep(poll_s(2))
    return held


if __name__ == "__main__":
    sys.exit(supervise(Path(sys.argv[1])))
