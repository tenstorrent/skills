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
  network stall are not progress; in a run whose spec sets `waits_idle` (a chat-route run), wait calls
  (a long sleep or a polling loop, is_wait) filling WAIT_SHARE of the last stall_s also end it, so a run
  that only sleep-polls between quick checks stalls too;
- enforces the run's dollar budget mid-flight when the provider streams usage;
- in a task run the harness hook does not reach (streamguard), checks each tool call the agent starts
  as the hook would and stops the run at the first one it refuses;
- ends the child when <run_dir>/STOP appears (a cancel, or `ttp stop --kill` writing "shutdown") or
  the run dir is deleted, and never starts it when the STOP came first;
- ends the child's whole process group, its tools too, TERM then KILL, when the runner itself dies
  (killed outright): a watchdog process does it on every platform, and on Linux the kernel also
  sends the child SIGTERM at once (PR_SET_PDEATHSIG);
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
- before a task that pins its model or effort starts its agent, asks the agent which model and
  effort its session resolves (no model turn) and refuses the run on a mismatch (check_pin);
- writes exit.json exactly once, then asks the provider adapter for usage and records it.

It survives a daemon restart: the daemon re-adopts runs by run_dir, pid and boot id.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import loopproxy, poll_s, project
from .project import Project, durable_write, lower_priority

LEASE_EVERY_S = 30
KILL_AFTER_S = 30
PRE_GRANT_EVERY_S = 30   # a whole-run take that waits asks the project's pre-grant check again this often
PRE_GRANT_FRESH_S = 3    # a pre-grant answer older than this is asked again once the take has the slot
POLL_S = 5
WATCH_ERRORS_LOGGED = 20   # a watch error that repeats every poll is logged this many times
BUDGET_EVERY_S = 10
# A run's transcript is read for the system prompt it passed this often until there is a verdict
# (check_instructions); one not found this long into the run is reported, never stopped on.
INSTRUCTIONS_EVERY_S = 5
ROLLOUT_WAIT_S = 120
TOOL_GRACE_S = 60     # past a pending tool call's own timeout before its silence counts as a stall
WAIT_SHARE = 0.9      # waits_idle: this share of stall_s spent in wait calls is a stall
TEST_CLOCK_ENV = "TTP_TEST_AWAKE_FILE"   # the test suite's FileClock; unset (always, in real use): AwakeClock
SLEEP_GAP_S = 600     # polls are POLL_S apart: a gap this long between two can only be a host sleep
PDEATHSIG = sys.platform.startswith("linux")   # the kernel also signals the agent itself if the runner dies
PR_SET_PDEATHSIG = 1

# Started with every agent: waits on a pipe the runner holds open. A word on it is a normal end; end of
# file without one means the runner died, and the agent's group is ended, TERM then KILL. PDEATHSIG
# alone is not enough: it signals only the agent, not the tools in its group, and never escalates.
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


BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")


def _sysctl(key: str) -> str:
    try:
        return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _boottime_secs() -> str:
    """Whole seconds of kern.boottime (macOS); the microseconds field drifts between reads. "" if none."""
    try:
        return _sysctl("kern.boottime").split("sec =")[1].split(",")[0].strip()
    except IndexError:
        return ""


def boot_id() -> str:
    try:
        return BOOT_ID_FILE.read_text().strip()
    except OSError:
        pass
    # macOS: kern.bootsessionuuid is new each boot. kern.boottime moves when the clock is stepped, so
    # it is only the fallback where the uuid is missing.
    return _sysctl("kern.bootsessionuuid") or _boottime_secs() or "unknown"


def boot_ids() -> tuple[str, str]:
    """This boot's id, then the id older versions recorded for it (macOS: kern.boottime's seconds,
    before kern.bootsessionuuid was preferred); the same id twice where there is only one."""
    b = boot_id()
    if b == "unknown" or BOOT_ID_FILE.exists():
        return b, b
    return b, _boottime_secs() or b


def same_boot(recorded: str | None) -> bool:
    """Whether a recorded boot id names this boot, in either the current or the older form."""
    return bool(recorded) and recorded in boot_ids()


def boot_time() -> float | None:
    """When this host booted, as a Unix time; None if it cannot be told."""
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError):
        pass
    try:
        return float(_boottime_secs())
    except ValueError:
        return None


def proc_start(pid: int) -> str | None:
    """When process pid started, as a token that stays the same for its whole life and differs for
    a later process given the same pid. None if it is gone. Never forks where there is /proc (a pid
    missing there is gone: ps reads the same /proc); without it, project.ps_snapshot's one `ps` inside one."""
    if project._PROC:
        try:   # Linux: clock ticks from boot to its start (field 22)
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None
    if (rows := project.ps_rows()) is not None:
        if pid in rows:
            return rows[pid][1]
        try:   # not in the snapshot: gone, or started since it was taken (then asked alone)
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except OSError:
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
    """Ask a run's supervisor to end its agent (TERM, then KILL after the grace period).
    Written whole by rename: a supervisor polling between create and write would read "" and
    record a project stop ("shutdown") as a cancel."""
    durable_write(run_dir / "STOP", why)


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
    """"shutdown" when the whole project was stopped, or the run preempted for urgent work or restarted (preempt):
    the task goes back to the queue; else "stopped" (a cancel). None while there is no STOP file."""
    try:
        text = (run_dir / "STOP").read_text().strip()
    except OSError:
        # The run dir itself is gone (deleted under a live run, e.g. a test's temp dir): nobody can
        # read its output or hand-off any more, so the agent must not run on unattended.
        return None if run_dir.is_dir() else "stopped"
    return "shutdown" if text in ("shutdown", "preempt", "restart") else "stopped"


def _write_exit(run_dir: Path, info: dict) -> None:
    """Record how the run ended; never creates a run dir that was deleted (durable_write would)."""
    if run_dir.is_dir():
        try:
            durable_write(run_dir / "exit.json", json.dumps(info))
        except FileNotFoundError:
            pass


def launch_error(e: OSError, argv: list[str], env: dict) -> str:
    """Why the agent could not be started, with the command line's size where that is the cause."""
    why = f"could not start the agent: {e}"
    if e.errno == errno.E2BIG:
        sizes = [len(os.fsencode(a)) + 1 for a in argv]
        total = sum(sizes) + sum(len(os.fsencode(f"{k}={v}")) + 1 for k, v in env.items())
        longest = max(range(len(argv)), key=sizes.__getitem__) if argv else 0
        why += (f" (command line {total} bytes; longest argument #{longest}, {sizes[longest] - 1} bytes"
                f"{', after ' + argv[longest - 1] if longest else ''})")
    return why[:400]


def _agent_preexec(nice: int):
    """The agent's preexec_fn and whether it arms PR_SET_PDEATHSIG: its nice level, and on Linux a
    SIGTERM from the kernel to the agent the moment the runner dies (the watchdog ends the rest of
    its group). The signal follows the thread that started the child, here the main thread, which
    lives as long as the runner."""
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
    """A process of its own that ends the agent's process group when the runner dies. Returns the pipe's write end and the process (None, None if it cannot start)."""
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
    Only a dir directly inside the run dir, or a short one (shorttmp), is ever removed."""
    from . import shorttmp
    tmp = spec.get("tmp_dir")
    if shorttmp.owned(tmp):
        shorttmp.remove(tmp)
    elif tmp and Path(tmp).parent.resolve() == run_dir.resolve() and not Path(tmp).is_symlink():
        shutil.rmtree(tmp, ignore_errors=True)


def remove_private(run_dir: Path) -> None:
    """Delete the run's private files (a per-run MCP config may carry credentials) and temp dir."""
    try:
        spec = json.loads((run_dir / "run.json").read_text())
        remove_files(spec.get("private_files") or [])
        remove_tmp(run_dir, spec)
    except (OSError, ValueError, AttributeError):
        pass
    from . import cursorhook
    cursorhook.undo(run_dir)   # a Cursor run's hooks file (none for other agents)


def check_instructions(prov, run_dir: Path, out_path: Path, argv: list[str], env: dict, waited_s: float) -> dict | None:
    """One look at whether the agent took the system prompt the run passed it (system.md, by
    append_system_args): a verdict to record ({"state": "recorded" | "missing" | "no_rollout" |
    "unchecked" (the transcript still pending after ROLLOUT_WAIT_S), ...}),
    {} when there is nothing to check, None to look again. A transcript that is missing or unreadable
    is only reported, as its format or path may change."""
    try:
        text = (run_dir / "system.md").read_text()
    except (OSError, ValueError):
        return {}   # no system prompt passed (or none readable to compare)
    try:
        got = prov.instructions_recorded(out_path, argv, text, env)
    except Exception as e:   # also after the run ended, where nothing else would catch it
        got = {"state": "unreadable", "error": f"{type(e).__name__}: {e}"[:200]}
    if not isinstance(got, dict):
        return {}
    if got.get("state") in ("recorded", "missing"):
        return got
    if got.get("state") in ("no_session", "no_rollout", "unreadable") and waited_s >= ROLLOUT_WAIT_S:
        return {**got, "state": "no_rollout", "seen": got.get("state")}
    if got.get("state") == "pending" and waited_s >= ROLLOUT_WAIT_S:
        return {**got, "state": "unchecked"}   # no model reply yet: reported once, not re-parsed all run
    return None


def _cred_snapshot() -> dict | None:
    """credwatch.snapshot() of the account's credential files; None when it fails (the run goes on)."""
    try:
        from . import credwatch
        return credwatch.snapshot()
    except Exception as e:
        print(f"runner: could not stat the credential files: {type(e).__name__}", flush=True)
        return None


def _env_files(spec: dict) -> dict[str, str]:
    """The secret environment the daemon kept out of run.json (env_files: owner-only JSON files,
    removed with the run's private files), e.g. Codex workers' MCP server tokens."""
    out: dict[str, str] = {}
    for path in spec.get("env_files") or []:
        try:
            got = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue   # gone (a rerun after the run ended): the servers start without them
        if isinstance(got, dict):
            out.update({str(k): str(v) for k, v in got.items()})
    return out


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
    # The secret files go under the daemon's environment: a server's env never changes the worker's
    # PATH, PYTHONPATH, TTP_* or device mask.
    env = {**os.environ, **_env_files(spec), **env_extra}
    # A proxy on `localhost` as 127.0.0.1 (loopproxy): in memory only, the values may carry credentials.
    env.update(loopproxy.fixed(env))
    if spec.get("waits_idle"):   # its hook turns wait calls back to a waiting hand-off (hook.idle_wait)
        env[WAITS_IDLE_ENV] = "1"
    lease, out_path = run_dir / "lease", run_dir / "output.jsonl"
    started = time.time()
    if spec.get("pin") and not stop_reason(run_dir):
        refused = check_pin(run_dir, spec, env)
        if refused:
            remove_files(spec.get("private_files") or [])
            remove_tmp(run_dir, spec)
            _write_exit(run_dir, {"rc": None, "started": started, "ended": time.time(), "launched": False,
                                  "model_turn_started": False, **refused})
            return 1
    # A stop that came before the agent started (a cancel right after start_run): it never starts.
    held = None if stop_reason(run_dir) else []
    inherited: list[str] = []
    if held is not None:
        _touch(lease)
        wait_s = min(float(spec.get("exclusive_wait_s") or 600), timeout_s)
        held = _take_exclusive(run_dir, spec.get("exclusive") or [], spec.get("env", {}), started + wait_s,
                               spec.get("holds"), inherited, spec.get("pre_grant"))
        if held:
            from . import onhost
            onhost.record(run_dir, [w.get("resource") for w in spec.get("exclusive") or [] if isinstance(w, dict)])
    def let_go(held: list) -> None:
        if held:
            # The slots may have been handed over from an earlier run's keeper: its jobs keep them.
            try:
                from . import hold
                hold.keep(run_dir, spec, held, spec.get("exclusive") or [], inherited)
            except Exception as e:
                print(f"runner: could not keep the run's resources for its detached jobs: {e}", flush=True)
        for h in held:
            h.close()

    if held is not None and stop_reason(run_dir):
        let_go(held)
        held = None
    if held is None:
        remove_files(spec.get("private_files") or [])
        remove_tmp(run_dir, spec)
        _write_exit(run_dir, {"rc": None, "started": started, "ended": time.time(),
                              "stopped": stop_reason(run_dir) or "resource_busy", "launched": False})
        return 1
    started, mono_start = time.time(), time.monotonic()
    awake = FileClock(os.environ[TEST_CLOCK_ENV]) if os.environ.get(TEST_CLOCK_ENV) else AwakeClock()
    prompt = open(run_dir / (spec.get("stdin") or "prompt.md"), "rb")
    out = open(out_path, "wb")
    err = open(run_dir / "stderr.log", "wb")
    nice = int(spec.get("nice") or 0)
    preexec, _ = _agent_preexec(nice)
    bridge, launch_auth = None, False
    try:
        if spec.get("bridge"):
            # The chat provider's loopback bridge: a thread of this process, so it ends with the run
            # however the run ends. The agent gets its URL and a per-run token, never the endpoint's key.
            from .chatbridge import Bridge, BridgeError
            try:
                bridge = Bridge.from_settings(spec["bridge"]).start()
            except (BridgeError, ValueError, OSError) as e:
                launch_auth = bool(getattr(e, "auth", False))   # no usable key: the auth gate's (daemon)
                raise OSError(f"chat bridge: {e}") from None
            env.update(bridge.env())
        # The stream guard reads the hook's marker before the agent starts: it could write one itself.
        from .hook import hooked
        pre_hooked = hooked(run_dir)
        creds = _cred_snapshot()
        child = subprocess.Popen(argv, stdin=prompt, stdout=out, stderr=err, cwd=cwd, env=env,
                                 start_new_session=True, preexec_fn=preexec)
    except OSError as e:
        # The agent never started (E2BIG: an argument or the whole command line past the kernel's
        # limit; a missing binary; a chat bridge that could not start): a harness fault the same
        # argv repeats, booked as a launch failure.
        if bridge is not None:
            bridge.stop()
        for f in (prompt, out, err):
            f.close()
        let_go(held)
        remove_files(spec.get("private_files") or [])
        remove_tmp(run_dir, spec)
        _write_exit(run_dir, {"rc": None, "started": started, "ended": time.time(), "launched": False,
                              "launch_error": launch_error(e, argv, env),
                              **({"launch_auth": True} if launch_auth else {})})
        return 1
    guard, watchdog = _start_watchdog(child.pid)
    (run_dir / "child.pid").write_text(f"{child.pid}\n{proc_start(child.pid) or ''}\n")
    niceness = _niceness(child.pid)
    if nice and niceness is not None and niceness < min(os.nice(0) + nice, 19):
        print(f"runner: could not lower the agent's priority by {nice} (it runs at nice {niceness})", flush=True)
    reason: list[str] = []
    # The system prompt check (check_instructions): open until it has a verdict.
    inst: dict = {"open": True, "at": 0.0, "got": None, "prov": None}

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
        inst["prov"] = prov
        st = {"lease": 0.0, "budget": 0.0, "active": 0.0}
        progress = ProgressWatch(out_path, run_dir)
        waits_idle = bool(spec.get("waits_idle"))
        from . import streamguard
        guard = {"on": streamguard.active(prov, argv, env, pre_hooked), "offset": 0}

        def check_calls() -> None:
            # A run the harness hook does not reach: each tool call it starts is checked as the hook would.
            # Decided once, from the marker as it was before launch: the agent could write one itself.
            calls, guard["offset"] = prov.activity(out_path, guard["offset"])
            for c in (c for c in calls if c.get("kind") == "call"):
                for payload in c.get("payloads") or []:
                    why = streamguard.refusal(payload, env, cwd)
                    if why:
                        guard["on"] = False
                        streamguard.record(run_dir, why, str(payload.get("tool_name") or ""))
                        print(f"runner: stream guard stopped the run at a {payload.get('tool_name')} call: {why}",
                              flush=True)
                        threading.Thread(target=stop, args=("guard",), daemon=True).start()
                        return

        def look() -> None:
            now, up = time.time(), awake.tick()
            if now - st["lease"] >= LEASE_EVERY_S:
                try:
                    _touch(lease)
                except OSError:   # the run dir is gone: stop_reason below ends the agent
                    pass
                st["lease"] = now
            if up > timeout_s + min(locks.waited(run_dir), timeout_s):
                threading.Thread(target=stop, args=("timeout",), daemon=True).start()
            if stall_s:
                # Stalled = no assistant message, tool result or progress note for stall_s of awake
                # time, or for a pending tool call's own timeout (plus grace) when that is longer.
                if progress.poll(up):
                    st["active"] = up
                if up - st["active"] > progress.limit(stall_s) or (
                        waits_idle and progress.waited(up, stall_s) >= WAIT_SHARE * stall_s):
                    threading.Thread(target=stop, args=("stalled",), daemon=True).start()
            if budget is not None and now - st["budget"] >= BUDGET_EVERY_S:
                st["budget"] = now
                try:
                    so_far = prov.cost_so_far(out_path, since=started, env=env)
                except Exception:
                    so_far = None
                if so_far is not None and so_far > float(budget):
                    threading.Thread(target=stop, args=("budget",), daemon=True).start()
            if inst["open"] and now - inst["at"] >= INSTRUCTIONS_EVERY_S:
                inst["at"] = now
                got = check_instructions(prov, run_dir, out_path, argv, env, up)
                if got is not None:
                    inst.update(open=False, got=got or None)
                    if got.get("state") == "missing" and env_extra.get("TTP_TASK"):
                        # Running without the charter's restrictions: ended now, its task blocked (daemon).
                        threading.Thread(target=stop, args=("instructions_missing",), daemon=True).start()
            if guard["on"]:
                check_calls()
            why = stop_reason(run_dir)
            if why:
                threading.Thread(target=stop, args=(why,), daemon=True).start()

        errors = 0
        while child.poll() is None:
            try:
                look()
            except Exception:
                # One failed look must not end the only thread that enforces the limits: log it, go on.
                errors += 1
                if errors <= WATCH_ERRORS_LOGGED:
                    print(f"runner: watch error {errors}: " + traceback.format_exc().replace("\n", " | ")[:2000],
                          flush=True)
            time.sleep(poll_s(POLL_S))

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    while True:
        try:
            rc = child.wait(timeout=poll_s(POLL_S))
            break
        except subprocess.TimeoutExpired:
            pass
        if not t.is_alive() and child.poll() is None:
            # The watch thread died anyway: nothing enforces the wall clock, stall, budget or cancel.
            print("runner: the watch thread died; ending the agent", flush=True)
            stop("supervisor_error")
            rc = child.wait()
            break
    ended, mono_end, up = time.time(), time.monotonic(), awake.tick()
    if inst["open"]:
        # A run that ended before a verdict: one last look, which never reports a missing transcript.
        # A quick run can end before the watch thread built its provider: build it here then.
        try:
            if inst["prov"] is None:
                from .providers import get_provider
                inst["prov"] = get_provider(spec["provider"]).use(spec.get("model", ""), spec.get("prices"))
            got = check_instructions(inst["prov"], run_dir, out_path, argv, env, 0.0)
            inst["got"] = got or None
        except Exception as e:
            print(f"runner: could not check the run's system prompt: {e}", flush=True)
    if bridge is not None:
        bridge.stop()
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
    if inst["got"]:
        exit_info["instructions"] = {k: v for k, v in inst["got"].items() if k in ("state", "cli_version", "seen")}
        if inst["got"].get("state") == "missing" and env_extra.get("TTP_TASK"):
            ver = inst["got"].get("cli_version") or "unknown version"
            exit_info.update(refused="instructions_missing", error=(
                f"{spec['provider']} ({ver}) did not record the run's system prompt in its transcript, so the "
                f"run went without its binding restrictions; "
                + ("it was stopped" if reason and reason[0] == "instructions_missing" else "its result is held")))
            print(f"runner: {exit_info['error']}", flush=True)
    if creds is not None and (after := _cred_snapshot()) is not None:
        from . import credwatch
        if changed := credwatch.changes(creds, after):
            exit_info["cred_changed"] = changed   # the daemon queues the observation (credwatch.observe)
            # Stat marks only (size, mtime, inode, ctime): when each changed, for credwatch.classify.
            exit_info["cred_marks"] = {p: [creds["marks"].get(p), after["marks"].get(p)] for p, _ in changed}
            print(f"runner: credential files changed during the run: {changed}", flush=True)
    if bridge is not None:
        exit_info["bridge"] = bridge.stats()   # endpoint fault kinds for routing's fallback (route.py)
    try:   # before exit.json: the daemon reads the run's output and hand-off only after it
        from . import redact
        redact.scrub_run(run_dir)
    except Exception as e:
        print(f"runner: could not mask stored secrets in the run: {type(e).__name__}", flush=True)
    _write_exit(run_dir, exit_info)
    return rc


def check_pin(run_dir: Path, spec: dict, env: dict) -> dict | None:
    """Before a task that pins its model or effort starts its agent: ask the agent which model and
    effort the session resolves (no model turn), recording the outcome in run.json (`session`).
    Mismatches always refuse; unverifiable pins refuse only with pin_verify=strict."""
    from .providers import get_provider
    pin = {k: str(v) for k, v in (spec.get("pin") or {}).items() if k in ("model", "effort") and v}
    mode = spec.get("pin_verify", "lenient")
    try:
        got = get_provider(spec["provider"]).resolved_session(spec["argv"], spec["cwd"], env)
    except Exception as e:
        got = {"error": f"{type(e).__name__}: {e}"[:300]}
    if got is None:
        got = {"error": "provider cannot report its resolved session before its first model turn"}
    elif not isinstance(got, dict):
        got = {"error": "provider returned an invalid session response"}
    # Preserve existing mismatch handling, including an unset resolved model or effort. An
    # error response cannot establish either a match or a mismatch.
    off = [] if got.get("error") else [f"{k} {got.get(k) or 'unset'} (pinned {v})" for k, v in pin.items()
                                      if str(got.get(k) or "").lower() != v.lower()]
    outcome = "mismatched" if off else "unverifiable" if got.get("error") else "verified"
    session = {**got, "checked": time.time(), "verification": outcome, "pin_verify": mode}
    try:
        durable_write(run_dir / "run.json", json.dumps({**spec, "session": session}, indent=1))
    except OSError:
        pass
    if outcome == "unverifiable":
        why = f"could not verify the pinned {', '.join(pin)} for {spec['provider']}: {got['error']}"
        print(f"runner: {why} (pin_verify={mode})", flush=True)
        if mode == "strict":
            why += "; pin_verify=strict refused the run before its first model turn"
            return {"refused": "pin_unverified", "error": why, "session": session}
        return None
    if not off:
        return None
    why = f"{spec['provider']} resolved {' and '.join(off)}; the run was refused before its first model turn"
    print(f"runner: {why}", flush=True)
    return {"refused": "model_mismatch", "error": why, "session": session}


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
WAIT_MIN_S = 30   # a sleep this long or longer makes a command a wait (is_wait)
WAITS_IDLE_ENV = "TTP_WAITS_IDLE"   # set to 1 in a waits_idle run's agent: its hook refuses wait calls
_SLEEP = re.compile(r"(?<![\w.-])sleep\s+(\d+(?:\.\d+)?)([smhd]?)(?![\w.])|time\.sleep\(\s*(\d+(?:\.\d+)?)")
_LOOP = re.compile(r"(?<![\w.-])(?:while|until|for)\b[^\n]*?(?:;|\n)\s*do\b")
# A command that blocks without sleeping until its timeout: `tail -f` and `watch` (also `gh run watch`),
# as a command of its own, not a word in another command's arguments (`grep -n watch`).
_FOLLOW = re.compile(r"(?:^|[;&|(`\n]|\b(?:do|then)\s|\btimeout(?:\s+-\S+)*\s+\d\S*\s)\s*"
                     r"(?:tail\b[^;&|\n]*?\s(?:-[A-Za-z0-9]*[fF][A-Za-z0-9]*|--follow\S*)(?!\S)"
                     r"|(?:gh\s+run\s+)?watch\s)")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def is_wait(command) -> bool:
    """Whether a shell command only waits: a sleep of WAIT_MIN_S or more, a loop that sleeps (a poll),
    or a command that blocks until its timeout without sleeping (`tail -f`, `watch`). A command that
    returns at once (`timeout 550 ttp checks --result <dir>`) is no wait."""
    if not isinstance(command, str):
        return False
    sleeps = [float(a or c) * _UNIT[u] for a, u, c in _SLEEP.findall(command)]
    return bool(sleeps) and (max(sleeps) >= WAIT_MIN_S or bool(_LOOP.search(command))) \
        or bool(_FOLLOW.search(command))


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


class FileClock:
    """Test only (TEST_CLOCK_ENV names the file): the awake time is the number in that file, which a fake
    agent advances, so the limit and the stall guard see the time the test lets pass, not a loaded
    host's scheduling delays. A missing or unreadable file keeps the last reading; it never goes back."""

    def __init__(self, path: str):
        self.path, self.awake = Path(path), 0.0

    def tick(self) -> float:
        try:
            self.awake = max(self.awake, float(self.path.read_text()))
        except (OSError, ValueError):
            pass
        return self.awake


class ProgressWatch:
    """Reads the agent's stream as it grows and tells whether it made progress since the last look:
    an assistant message, a tool result, streamed thinking or a new progress note. Lines that are not
    JSON, and event types it does not know (other providers), count as progress. It also tracks the
    tool calls still waiting for their result and the timeouts they set themselves, and, given the
    awake time of each poll, when its wait calls (is_wait) ran."""

    def __init__(self, out_path: Path, run_dir: Path):
        self.out_path, self.note_path = out_path, run_dir / "progress.md"
        self.offset, self.partial = 0, b""
        self.note = self._note_mark()
        self.pending: dict[str, float] = {}   # tool_use id -> its own timeout in seconds (0 = none)
        self.up: float | None = None          # the awake time of this poll
        self.open_waits: dict[str, float] = {}   # wait call id -> when it started
        self.waits: list[tuple[float, float]] = []   # (start, end) of finished wait calls

    def _note_mark(self):
        try:
            st = self.note_path.stat()
            return (st.st_size, st.st_mtime)
        except OSError:
            return None

    def poll(self, up: float | None = None) -> bool:
        self.up = up
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

    def waited(self, up: float, window: float) -> float:
        """Seconds of the last `window` of awake time that wait calls ran (overlaps count once)."""
        since = up - window
        self.waits = [w for w in self.waits if w[1] > since]
        spans = sorted([*self.waits, *((s, up) for s in self.open_waits.values())])
        total, reach = 0.0, since
        for s, e in spans:
            s = max(s, reach)
            if e > s:
                total, reach = total + e - s, e
        return total

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
                if self.up is not None and is_wait(inp.get("command")):
                    self.open_waits[block["id"]] = self.up
            elif block.get("type") == "tool_result":
                self.pending.pop(block.get("tool_use_id"), None)
                start = self.open_waits.pop(block.get("tool_use_id"), None)
                if start is not None:
                    self.waits.append((start, self.up))
        return True


def _clock(env: dict) -> str:
    """'23:32:05 UTC': now in the home zone of the project in `env` (TTP_PROJECT); UTC without one."""
    from . import timefmt
    return timefmt.clock(env)


def _take_exclusive(run_dir: Path, wanted: list[dict], env: dict, deadline: float, holds: str | None = None,
                    inherited: list | None = None, pre_grant: dict | None = None) -> list | None:
    """One slot of each resource, waiting while `ttp lock` commands hold them all. The daemon starts
    an exclusive task only when a slot is free, so a wait here is a race it lost; the resource stays
    reserved while it waits, so new `ttp lock` commands let it in. A slot an earlier run of the same
    task keeps for its detached jobs (hold.py) is handed over, and those jobs go to `inherited`. The
    wait ends at the deadline or on a stop; None when it ended without the slots. With `pre_grant` (the
    project's pre-grant check, pregrant.py) each slot is tried only while the check grants it, asked
    again every PRE_GRANT_EVERY_S of a wait and once more when the slot is taken if its last answer is
    older than PRE_GRANT_FRESH_S (a hold that began during the wait); a check that holds ends the wait at
    once, without the slots, so the task goes back to dispatch, which holds it model-free until the check
    grants it."""
    from . import hold, locks
    task = hold.who(env.get('TTP_TASK'))
    held, told, asked = [], 0.0, set()
    where = Project(env["TTP_PROJECT"]) if env.get("TTP_PROJECT") else None   # holders' times in its zone
    for res in wanted:
        paths = [Path(x) for x in res["paths"]]
        mark = Path(res["reserve"]) if res.get("reserve") else None
        # A shared resource's holder names the project as well (shared.holder).
        mine = res.get("holder") or task
        asked_at = None

        def _held_by_check() -> bool:
            # The project's own holds: True (and the run does not start) when the check does not grant.
            nonlocal asked_at
            from . import pregrant
            asked_at = time.time()
            got = pregrant.check(pre_grant["cmd"], pre_grant["root"], res["resource"], env.get("TTP_TASK") or "",
                                 pre_grant.get("state"), float(pre_grant.get("timeout_s") or pregrant.TIMEOUT_S))
            if got.granted:
                return False
            for h in held:
                h.close()
            if mark:
                locks.unreserve(mark, mine)
            with project.open_append(run_dir / "progress.md") as pf:
                pf.write(f"{_clock(env)} {got.line}; the run did not start\n")
            return True

        while True:
            if pre_grant and (asked_at is None or time.time() - asked_at >= PRE_GRANT_EVERY_S) and _held_by_check():
                return None
            # Not in the `ttp lock` arrival queue: the reservation below already holds new commands
            # off, and a queued command that is waiting on the reservation must not hold this off.
            f = locks.try_take(paths, hold.label(mine, env.get("TTP_RUN_ID"), hold.WHOLE_RUN), "exclusive")
            if f and pre_grant and time.time() - asked_at >= PRE_GRANT_FRESH_S:
                held.append(f)   # asked again holding the slot, so no hold can begin between the answer and the take
                if _held_by_check():
                    return None
                held.pop()
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
                with project.open_append(run_dir / "progress.md") as pf:
                    pf.write(f"{_clock(env)} waiting for {res['resource']} "
                             f"(held by {', '.join(locks.holders_now(paths, where)) or 'another task'})\n")
                told = time.time()
            time.sleep(poll_s(2))
    return held


if __name__ == "__main__":
    sys.exit(supervise(Path(sys.argv[1])))
