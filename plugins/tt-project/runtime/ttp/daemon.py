# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The per-project daemon. Deterministic and cheap: it owns time (schedules, watchers), money
(meters, gates), state (tasks, runs, messages) and I/O (web app, Slack, chats). It calls a model
only through a run: a coordinator turn when something needs a decision, or a worker for a task.

Invariants:
- at most one coordinator turn at a time; turns are batched, debounced and rate-capped;
- no run starts while its provider's gate forbids it; every run has a wall clock and a budget;
- a run's result is read from disk, never from a live pipe, so a daemon restart loses nothing;
- a missed schedule fires once on wake, never once per missed slot.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from . import alerts
from . import budget as bud
from . import coordinator as coord
from . import locks
from . import machines
from . import release
from . import runner
from . import schedule as sched
from . import screen as scr
from . import worktree
from .db import SEVERITY_RANK, TERMINAL_TASK_STATES, continues_id, dependency_ids, dump_result, load_result
from .project import Project, hostname, load_secrets
from .providers import get_provider
from .providers.base import last_json_object, scratch_dir, service_path
from .providers.claude import as_windows
from .providers.jev import Jev, JevOutOfFunds

TICK_S = 3.0
LEASE_STALE_S = 180
HEARTBEAT_STALE_S = 300   # longer than any single tick step (a git fetch, a watcher command)
WATCHDOG_S = 2 * HEARTBEAT_STALE_S   # no tick progress this long: the service restarts the daemon
PROGRESS_EVERY_S = 30   # how often a long tick tells the watchdogs it is still moving
WATCHER_MAX_S = HEARTBEAT_STALE_S - 60   # a command watcher's timeout_s is capped here, well below WATCHDOG_S
RESULT_FILE = "result.json"
# What a waiting hand-off keeps across a run the account refused (limit, auth).
WAIT_KEYS = ("retry_when", "retry_after_s", "waiting_for", "wake_tier", "survives_reboot", "waits",
             "waiting_since")
MAX_FOLLOWUPS, FOLLOWUP_SPEC_CHARS = 12, 4000   # per hand-off; each follow-up is its own event
PROBE_EVERY_S = 180     # how often a waiting task's `retry_when` probe runs
PROBE_TIMEOUT_S = 60
ORPHAN_GRACE_S = 10     # TERM to KILL for an agent whose supervisor died
HANDOFF_STATES = ("done", "blocked", "failed", "needs_review", "waiting")
SLEEP_CUT = ("timeout", "stalled", "lost", "failed")   # ends a host sleep can cause
DISK_LIGHT_KINDS = ("question", "plan")   # the only task kinds that still start under the disk guard
DISK_RESUME = 1.2        # the guard ends once free space is this many times its threshold
DISK_FLOOR_GB = 2        # below this even questions and plans wait
KEEP_RECHECK_S = 6 * 3600   # a finished task's kept worktree is looked at again this often
SLEPT_MIN_S = 60        # a run whose wall clock ran this much ahead of its monotonic clock overlapped a host sleep
SLEEPS_KEPT_S = 7 * 86400


def log(p: Project, msg: str) -> None:
    p.logs.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n"
    with open(p.logs / "daemon.log", "a") as f:
        f.write(line)


class Daemon:
    def __init__(self, base: str | Path):
        self.p = Project(base)
        self.stopping = False
        self.boot = runner.boot_id()
        self.boot_at = runner.boot_time()
        self.gates: dict[str, bud.Gate] = {}
        self.cfg = self.p.config()
        self.jev = Jev(self.cfg, db=self.p.db)
        self._slack = None
        self._last_cfg = 0.0
        self._trouble_checked = 0.0
        self._last_slack = 0.0
        self._thread_scan = 0.0
        self._slack_rejects: dict[int, int] = {}   # outbound message id -> times Slack refused it
        self._reap_errors: dict[int, int] = {}
        self._metered: dict[int, tuple[int, float]] = {}   # run id -> (output size, when) last priced
        self._start_failures = 0
        self._lock_fd: int | None = None
        self._started = time.time()
        self._healthy = False
        self._progressed = 0.0   # when the daemon last told the watchdogs a tick step finished
        self._last_prune = 0.0
        self._release_due = 0.0   # when the installed tt-project release is next compared with the harness
        self._pruned_upto = 0.0   # the latest finish the last worktree sweep saw
        self._kept: dict[int, tuple[float, float, str]] = {}   # task id -> (task updated, checked, why kept)
        self._disk_low = bool(self.p.db.kv("disk_low"))   # an episode outlives a restart: no second alert
        self._disk_free: float | None = None
        self._tick_errors = 0
        self._probes: dict[int, tuple[subprocess.Popen, float]] = {}
        self._probed: dict[int, float] = {}
        self._probe_rc: dict[int, tuple[int | str, float]] = {}   # last verdict: exit code or why, when
        self._reboot_told = False
        self._boot_woken = False
        self._held: list[str] | None = None   # lock holders the heartbeat file last recorded
        self._notify: str | None = None   # systemd's socket for the watchdog ping, when it runs us
        self._tick_wall, self._tick_mono = time.time(), time.monotonic()
        self._settle_until = 0.0   # monotonic time before which nothing new starts (the host just woke)
        self._note_boot()

    # lifecycle ------------------------------------------------------------------------------------
    def run(self) -> int:
        self.p.state.mkdir(parents=True, exist_ok=True)
        pidfile = self.p.state / "daemon.pid"
        if not self._single_instance():
            print(f"daemon already running (pid {_read_pid(pidfile)})", file=sys.stderr)
            return 1
        pidfile.write_text(str(os.getpid()))
        # Kept here rather than in the environment, which runs and their tools would inherit.
        self._notify = os.environ.pop("NOTIFY_SOCKET", None)
        self._mark_start()
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stopping", True))
        log(self.p, f"daemon start pid={os.getpid()} host={hostname()} boot={self.boot}")
        self.p.db.set_kv("daemon", {"pid": os.getpid(), "host": hostname(), "started": time.time()})
        from .web import serve
        threading.Thread(target=serve, args=(self,), daemon=True).start()
        self._keep_awake()
        while not self.stopping:
            try:
                self.tick()
                self._beat()
            except Exception:  # a bad tick must never kill the daemon
                log(self.p, "tick error: " + traceback.format_exc().replace("\n", " | ")[:2000])
                if not self._healthy:
                    self._tick_errors += 1
                    self._mark_start()
                time.sleep(10)
            time.sleep(TICK_S)
        log(self.p, "daemon stop")
        if _read_pid(pidfile) == os.getpid():
            pidfile.unlink(missing_ok=True)
        # Running workers are left alone: they write their results to disk and the next start
        # adopts them. `ttp stop --kill` and cancels end them explicitly.
        return 0

    def _single_instance(self) -> bool:
        """Hold an exclusive lock on state/daemon.lock for the process lifetime: the kernel drops it
        when the process dies, so neither a race between two starters nor a recycled pid in a stale
        pid file can matter. Where the file system has no flock, fall back to the pid file, trusting
        it only if that pid is really a tt-project daemon."""
        held = _flock(self.p.state / "daemon.lock")
        if held is False:
            return False
        if held is not None:
            self._lock_fd = held
            return True
        other = _read_pid(self.p.state / "daemon.pid")
        return not (other and other != os.getpid() and _is_daemon(other))

    def _mark_start(self) -> None:
        """Record that this process started, and how often its first tick has failed, so `ttp restart`
        can tell a daemon that is alive but still in a slow first tick from one that is broken."""
        try:
            (self.p.state / "daemon.start").write_text(json.dumps(
                {"pid": os.getpid(), "host": hostname(), "started": self._started, "tick_errors": self._tick_errors,
                 "progress": self._progressed or None}))
        except OSError:
            pass

    def _note_boot(self) -> None:
        """On a new boot, keep what the earlier boot's last heartbeat said (when, and the resources
        held then) before this daemon's first tick overwrites it; the boot event is written from it
        once the runs the reboot cut short are reaped."""
        try:
            db = self.p.db
            if (db.kv("boot_prev") or {}).get("boot") == self.boot:
                return
            hb = heartbeat(self.p) or {}
            # An older runtime's heartbeat has no boot id; the boot last told about stands in.
            prev = hb.get("boot") or db.kv("reboot_told")
            if not prev or prev == self.boot:
                return
            db.set_kv("boot_prev", {"boot": self.boot, "prev_boot": prev, "held": hb.get("held") or [],
                                    "last_heartbeat": time.time() - hb["age"] if "age" in hb else None})
        except Exception:
            log(self.p, "boot record: " + traceback.format_exc().replace("\n", " | ")[:1000])

    def _beat(self) -> None:
        """A completed tick. `status`, the web app and `ttp restart` read its age; the first one
        marks the harness commit this runtime is known to run on. The file names the boot and the
        resources held, so the next boot can say what a reboot cut off; it is rewritten only when
        those change."""
        hb = self.p.state / "heartbeat"
        held = locks.held(self.p.state / "locks")
        if not self._healthy or held != self._held:
            tmp = hb.with_name(f"heartbeat.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"pid": os.getpid(), "host": hostname(), "started": self._started,
                                       "boot": self.boot, "held": held}))
            os.replace(tmp, hb)
            self._held = held
        else:
            os.utime(hb, None)
        sd_notify("WATCHDOG=1", self._notify)
        self._progressed = time.time()
        if not self._healthy:
            self._healthy = True
            try:
                head = subprocess.run(["git", "-C", str(self.p.harness), "rev-parse", "HEAD"], capture_output=True,
                                      text=True, timeout=30)
                if head.returncode == 0:
                    self.p.db.set_kv("harness_good", {"commit": head.stdout.strip(), "ts": time.time()})
            except (OSError, subprocess.SubprocessError):
                pass

    def _progress(self) -> None:
        """A tick step finished: tell systemd's WatchdogSec and `ttp.watchdog` the daemon still moves,
        so a long tick (a first tick that fetches and adds many worktrees, several slow watchers) is
        not taken for a stuck one. Before this daemon's first completed tick the heartbeat is left
        alone (`ttp restart` reads it as that tick); the start marker carries the progress instead."""
        now = time.time()
        if now - self._progressed < PROGRESS_EVERY_S:
            return
        self._progressed = now
        sd_notify("WATCHDOG=1", self._notify)
        if not self._healthy:
            self._mark_start()
            return
        try:
            os.utime(self.p.state / "heartbeat", None)
        except OSError:
            pass

    def tick(self) -> None:
        self._check_sleep()
        now = time.time()
        if now - self._last_cfg > 10:
            self.cfg, self._last_cfg = self.p.config(), now
            self.jev = Jev(self.cfg, db=self.p.db)
        for step in (self.reap_runs, self.wake_after_reboot, self.meter_running, self.reconcile_tasks,
                     self.prune_worktrees, self.check_disk, self.sweep_alerts, self.check_release):
            step()
            self._progress()
        if self.p.db.kv("paused", False):
            return
        self._refresh_meters()
        self.update_gates()
        coord.expire_asks(self.p, hold=any(g.level == "red" for g in self.gates.values()))
        settling = self.settling()
        for step in (self.run_schedules, self.poll_slack, self.check_resource_trouble, self.retry_rejected,
                     self.maybe_coordinate, self.probe_waiting, self.dispatch, self.deliver_outbound):
            # While the host settles after a sleep only new work waits: a person who wrote is answered now.
            if settling and (step == self.dispatch or step == self.maybe_coordinate and not self.p.db.one(
                    "SELECT id FROM messages WHERE direction='in' AND handled=0")):
                continue
            step()
            self._progress()

    def _check_sleep(self) -> None:
        """Notice that the host slept: the wall clock jumped ahead of the monotonic one, which stands
        still while the host sleeps. A long gap between ticks alone is not a sleep (a slow tick or a
        stopped daemon), and must not make the runs it overlapped free. Each sleep is kept for a week,
        so a run reaped as lost can tell it overlapped one, and starts the settle hold."""
        wall, mono = time.time(), time.monotonic()
        gap = wall - self._tick_wall
        jump = gap - (mono - self._tick_mono)
        since = self._tick_wall
        self._tick_wall, self._tick_mono = wall, mono
        if jump < SLEPT_MIN_S:
            return
        settle = float(self.cfg["budget"].get("wake_settle_s", 300))
        self._settle_until = mono + settle
        db = self.p.db
        sleeps = [s for s in db.kv("host_sleeps", []) or [] if s[1] >= wall - SLEEPS_KEPT_S]
        db.set_kv("host_sleeps", (sleeps + [[since, wall]])[-100:])
        db.set_kv("settle_until", wall + settle)   # for status: when new work starts, if it stays awake
        log(self.p, f"host slept for {jump:.0f} s (tick gap {gap:.0f} s); "
                    f"nothing new starts for {settle:.0f} s of awake time")

    def settling(self) -> bool:
        """The host woke from a sleep less than wake_settle_s of awake time ago: hold new runs."""
        return time.monotonic() < self._settle_until

    def _slept_between(self, start: float, end: float) -> bool:
        return any(a < end and b > start for a, b in self.p.db.kv("host_sleeps", []) or [])

    # runs -----------------------------------------------------------------------------------------
    def _approved_mcp(self, prov, provider: str, cwd: str) -> dict:
        """The project's allowlisted MCP servers (`providers.<p>.mcp_servers`) for an isolated run,
        looked up for the worktree and the project root."""
        names = coord.name_list(self.cfg["providers"].get(provider, {}).get("mcp_servers") or [])
        if not names:
            return {}
        dirs = list(dict.fromkeys(str(d) for d in (cwd, Path(cwd).resolve(), self.p.root, self.p.root.resolve())))
        found, unknown = prov.mcp_servers(names, dirs)
        if unknown:
            self.alert(f"mcp_servers_unknown:{provider}",
                       f"Workers run without these MCP servers, which your {provider} config does not define: "
                       f"{', '.join(unknown)}. Fix providers.{provider}.mcp_servers in project.json "
                       "(servers from a plugin cannot be listed).",
                       severity="low", every_s=86400)
        return found

    def start_run(self, role: str, prompt: str, provider: str, tier: str, cwd: str, *, task: dict | None = None,
                  budget_usd: float | None = None, timeout_s: float | None = None, read_only: bool = False,
                  schema: dict | None = None, system: str | None = None, append_system: str | None = None,
                  note: dict | None = None, resume: str | None = None) -> int:
        tiers = self.cfg["providers"].get(provider, {}).get("tiers", {})
        model = tiers.get(tier, {}).get("model", "")
        prices = (self.cfg.get("pricing") or {}).get(provider) or {}
        prov = get_provider(provider).use(model, prices)
        effort = tiers.get(tier, {}).get("effort", "")
        restrictions = self.cfg.get("restrictions", {})
        if read_only and prov.isolate_read_only:
            cwd = scratch_dir(str(self.p.base))
        argv, env = prov.build(role=role, model=model, effort=effort, cwd=cwd, budget_usd=budget_usd,
                               read_only=read_only, schema=schema, restrictions=restrictions)
        if role != "coordinator":
            window = self.cfg["budget"].get("compact_window_tokens") or 0   # 0: off for every tier
            window = window.get(tier) if isinstance(window, dict) else window
            env = {**env, **prov.compact_env(int(window or 0))}
            argv = _before_stdin(argv, prov.compact_args(int(window or 0)))
        mcp_servers: dict = {}
        resume_extra: list[str] = []
        private: list[str] = []   # files that may hold credentials, removed when the run ends
        if not read_only:
            # Skill plugins this project enabled for its workers only (never the user's own setup).
            dirs = [str(Path(os.path.expanduser(d))) for d in
                    coord.dir_list(self.cfg["providers"].get(provider, {}).get("plugin_dirs") or [])]
            missing = [d for d in dirs if not Path(d).is_dir()]
            if missing:
                self.alert(f"plugin_dirs_missing:{provider}",
                           f"Workers run without these {provider} plugin directories, which do not exist: "
                           f"{', '.join(missing)}. Fix providers.{provider}.plugin_dirs in project.json.",
                           severity="normal", every_s=86400)
            roots = [str(self.p.state)] + [d for d in [worktree.git_common_dir(Path(cwd))] if d]
            extra = prov.writable_args(roots) + prov.plugin_args([d for d in dirs if d not in missing])
            if self.cfg["providers"].get(provider, {}).get("worker_isolation"):
                extra += prov.isolation_args()
                mcp_servers = self._approved_mcp(prov, provider, cwd)
            # A continued session (see _resumable) gets the current system prompt again, below. Its
            # arguments go last: Codex takes them as a subcommand that must follow every option.
            resume_extra = prov.resume_args(resume) if resume else []
            argv = _before_stdin(argv, extra)
        db = self.p.db
        run_id = db.x("INSERT INTO runs(task,role,provider,model,effort,account,started,boot_id,status,note) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (task["id"] if task else None, role, provider, model, effort, prov.account(), time.time(),
                       self.boot, "running", json.dumps(note or {})))
        run_dir = self.p.runs / str(run_id)
        # Raising from here on means nothing was launched: the run row must not stay "running".
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            if mcp_servers:
                # Outside the repo and the run directory, owner-only: server entries can carry tokens.
                fd, mcp_path = tempfile.mkstemp(prefix=f"ttp-mcp-{run_id}-", suffix=".json")
                private.append(mcp_path)
                with os.fdopen(fd, "w") as f:
                    json.dump({"mcpServers": mcp_servers}, f)
                argv = prov.with_mcp_config(argv, Path(mcp_path))
            if system is not None:
                (run_dir / "system.md").write_text(system)
                if provider == "claude":
                    argv = _with_system_prompt(provider, argv, run_dir / "system.md")
                else:   # no replaceable system prompt: added to the agent's own, or leading the prompt
                    sys_args = prov.append_system_args(run_dir / "system.md")
                    if sys_args:
                        argv = _before_stdin(argv, sys_args)
                    else:
                        prompt = system + "\n\n" + prompt
            if append_system is not None:
                (run_dir / "system.md").write_text(append_system)
                sys_args = prov.append_system_args(run_dir / "system.md")
                if sys_args:
                    argv = _before_stdin(argv, sys_args)
                else:
                    prompt = append_system + "\n\n" + prompt
            argv = _before_stdin(argv, resume_extra)
            (run_dir / "prompt.md").write_text(prompt)
            runtime_dir = str(Path(__file__).resolve().parent.parent)
            env = {**env, "TTP_RUN_DIR": str(run_dir), "TTP_PROJECT": str(self.p.base), "TTP_RUN_ID": str(run_id),
                   "TTP_TASK": str(task["id"]) if task else "", "PYTHONPATH": runtime_dir,
                   "PATH": f"{self.p.harness / 'bin'}:{service_path()}:{os.environ.get('PATH', '')}"}
            tout = timeout_s or self.cfg["budget"]["run_timeout_s"].get(tier, 3600)
            stall = self.cfg["budget"].get("stall_s", {}).get(tier) if role != "coordinator" else None
            spec = {"argv": argv, "env": env, "cwd": cwd, "timeout_s": tout, "provider": provider, "stall_s": stall,
                    "model": model, "prices": prices,
                    "budget_usd": budget_usd if provider not in ("claude",) else None,
                    # What a run without its own budget is priced at when it reports no usage.
                    "default_budget_usd": self.cfg["budget"].get("task_default_usd", {}).get(tier, 8.0),
                    "exclusive": [{"resource": res, "paths": [str(x) for x in self._slot_paths(res)],
                                   "reserve": str(locks.reserve_path(self.p.state / "locks", res))}
                                  for res in _exclusive(task)] if task else [],
                    "exclusive_wait_s": self.cfg["budget"].get("exclusive_wait_s", 600),
                    "private_files": private}
            (run_dir / "run.json").write_text(json.dumps(spec, indent=1))
            with open(run_dir / "runner.log", "wb") as out:
                proc = subprocess.Popen([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=runtime_dir,
                                        env={**os.environ, "PYTHONPATH": runtime_dir}, stdout=out, stderr=out,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
        except BaseException:
            db.x("UPDATE runs SET status='failed', ended=? WHERE id=?", (time.time(), run_id))
            runner.remove_files(private)
            raise
        try:
            db.x("UPDATE runs SET pid=?, dir=? WHERE id=?", (proc.pid, str(run_dir), run_id))
        except Exception as e:   # launched all the same: the reaper finds it by its run id's directory
            log(self.p, f"run {run_id} started but its pid was not recorded: {e}")
        log(self.p, f"run {run_id} start role={role} provider={provider} tier={tier} task={task and task['id']}")
        return run_id

    def _run_dir(self, r: dict) -> Path:
        # A row whose start was cut short has no dir recorded; its directory is still named by its id.
        return Path(r["dir"]) if r["dir"] else self.p.runs / str(r["id"])

    def _run_alive(self, r: dict) -> bool:
        """The run's supervisor still lives: it renews the lease, or its process is up on this boot."""
        run_dir = self._run_dir(r)
        if (run_dir / "exit.json").exists():
            return False
        try:
            fresh = time.time() - (run_dir / "lease").stat().st_mtime <= LEASE_STALE_S
        except OSError:
            fresh = False
        return fresh or (r["boot_id"] == self.boot and bool(r["pid"]) and _alive(r["pid"]))

    def _last_sign_of_life(self, r) -> float:
        """When a lost run was last seen working, so downtime is not billed to it: the newest mtime
        of its lease and output, kept between its start and now. Now when neither file exists."""
        now, seen = time.time(), []
        for name in ("lease", "output.jsonl"):
            try:
                seen.append((self._run_dir(r) / name).stat().st_mtime)
            except OSError:
                pass
        if not seen:
            return now
        return min(now, max(max(seen), r["started"] or 0))

    def reap_runs(self) -> None:
        for r in self.p.db.q("SELECT * FROM runs WHERE status='running'"):
            try:
                exit_file = self._run_dir(r) / "exit.json"
                if exit_file.exists():
                    self.finish_run(r, _read_result(exit_file) or {"rc": -1, "stopped": "lost", "ended": time.time()})
                elif not self._run_alive(r):
                    self._end_orphan(r)
                    self.finish_run(r, {"rc": -1, "stopped": "lost", "ended": self._last_sign_of_life(r)})
                self._reap_errors.pop(r["id"], None)
            except Exception:
                # One run whose end cannot be processed must not hold up the others or wedge the loop.
                n = self._reap_errors[r["id"]] = self._reap_errors.get(r["id"], 0) + 1
                log(self.p, f"run {r['id']} reap error {n}: " + traceback.format_exc().replace("\n", " | ")[:2000])
                if n >= 3:
                    self._abandon_run(r)
        self._tell_reboot()

    def _tell_reboot(self) -> None:
        """Once per boot, after every run of an earlier boot is reaped: record the boot (when, the
        earlier boot's last heartbeat, the runs it cut short and the resources held then) and tell
        which runs it cut short, what they cost, what became of their tasks and how often the host
        rebooted lately. A restart on the same boot says nothing."""
        db = self.p.db
        if self._reboot_told or db.one("SELECT COUNT(*) n FROM runs WHERE status='running' AND boot_id IS NOT NULL "
                                       "AND boot_id!=?", (self.boot,))["n"]:
            return
        with db.tx():
            self._reboot_told = True
            if db.kv("reboot_told") == self.boot:
                return
            db.set_kv("reboot_told", self.boot)
            lost = [r for r in db.q("SELECT id, task, role, cost_usd, note FROM runs WHERE status='lost' "
                                    "AND boot_id!=? AND note LIKE ?", (self.boot, f"%{self.boot}%"))
                    if json.loads(r["note"] or "{}").get("lost_to_reboot") == self.boot]
            prev = db.kv("boot_prev") or {}
            if prev.get("boot") != self.boot:
                prev = {}
            if not lost and not prev:
                return   # the first start of this project, or nothing says the host rebooted
            now = time.time()
            usd = sum(float(r["cost_usd"] or 0) for r in lost)
            held = list(prev.get("held") or [])
            booted = self.boot_at
            data = {"boot": self.boot, "boot_time": booted, "prev_boot": prev.get("prev_boot"),
                    "last_heartbeat": prev.get("last_heartbeat"), "held": held, "lost_usd": round(usd, 2),
                    "lost": [{"run": r["id"], "task": r["task"], "role": r["role"],
                              "usd": round(float(r["cost_usd"] or 0), 2)} for r in lost]}
            # A record, not news: the coordinator sees it in its digest, not as a new event.
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status) VALUES(?,?,?,?,?,?,?,?)",
                 (min(booted or now, now), "host", "boot", f"boot:{self.boot}", "normal",
                  f"the host rebooted; {len(lost)} run(s) cut short", json.dumps(data), "record"))
            if not lost:
                return
            boots = db.boots(now - 86400)
            cut = sum(1 for b in boots if b.get("lost"))
            severity, unstable = "normal", ""
            if cut >= 3 and now - float(db.kv("host_unstable_told") or 0) >= 86400:
                db.set_kv("host_unstable_told", now)
                severity = "high"
                unstable = (f" The host looks unstable: {cut} reboots cut runs short in 24 h; check its power, "
                            f"cooling and system logs.")
            then = f" Held at its last heartbeat: {', '.join(held)}." if held else ""
            parts = []
            for r in lost:
                task = db.task(r["task"]) if r["task"] else None
                parts.append(f"run {r['id']} (#{task['id']} {task['title'][:60]} → {task['status']})" if task
                             else f"run {r['id']} ({r['role']})")
            nth = ordinal(max(len(boots), 1))
            # Information, never a "needs you" alert: the lost runs are already requeued.
            db.post("out", f"The host rebooted ({nth} reboot in 24 h); {len(lost)} run(s) were cut short "
                           f"(${usd:.2f}).{then}{unstable} Runs: {'; '.join(parts)}"[:3000],
                    kind="info", severity=severity, ref=f"reboot:{self.boot}")

    def _end_orphan(self, r: dict) -> None:
        """A supervisor that died (kill -9, OOM) leaves its agent running with no wall clock, budget
        or cancel, beside the retry of its task. End the agent's process group, TERM then KILL."""
        if r["boot_id"] != self.boot:
            return   # a reboot already ended it
        try:
            pid_text, _, started = (self._run_dir(r) / "child.pid").read_text().partition("\n")
            pid, started = int(pid_text), started.strip()
        except (OSError, ValueError):
            return
        if _is_agent(pid, started):
            log(self.p, f"run {r['id']} lost its supervisor; ending its agent (pid {pid})")
            _end_group(pid, ORPHAN_GRACE_S)

    def _abandon_run(self, r: dict) -> None:
        """Last resort for a run whose end keeps failing to process: close it so it cannot block the
        loop. Its task fails with the reason and the coordinator decides; a coordinator turn backs off."""
        db, now = self.p.db, time.time()
        why = f"run {r['id']} ended but its result could not be processed (details in the daemon log)"
        try:
            source = self._source_for(r)
        except Exception:
            source = f"task:{r['task']}" if r["task"] else r["role"]
        try:
            with db.tx():
                # Book what the run was priced at so far, once: only while it is still running.
                cur = db.one("SELECT * FROM runs WHERE id=? AND status='running'", (r["id"],))
                if not cur:
                    self._reap_errors.pop(r["id"], None)
                    return
                db.x("UPDATE runs SET status='failed', ended=? WHERE id=?", (now, r["id"]))
                cost = cur["cost_usd"] or 0
                if cost:
                    db.spend(r["provider"], cost, source, account=r["account"] or "",
                             estimated=bool(cur["cost_estimated"]), ts=now)
                    if r["task"]:
                        db.x("UPDATE tasks SET spent_usd=COALESCE(spent_usd,0)+? WHERE id=?", (cost, r["task"]))
                if r["role"] == "coordinator":
                    self._coordinator_failed(why)
                elif r["task"] and (db.task(r["task"]) or {}).get("status") == "running":
                    db.update_task(r["task"], status="failed", blocked_reason=why)
                    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                         (now, f"task:{r['task']}", "task_failed", "normal", f"#{r['task']}: {why}", "queued",
                          r["task"]))
            self._reap_errors.pop(r["id"], None)
            log(self.p, f"run {r['id']} abandoned after repeated reap errors")
        except Exception:
            log(self.p, f"run {r['id']} could not be abandoned: " + traceback.format_exc().replace("\n", " | ")[:2000])

    def reconcile_tasks(self) -> None:
        """Repair task states nothing else would. A task marked running with no live run (the daemon
        stopped between marking it and starting the run) goes back to the queue with no attempt spent.
        A queued task whose dependency failed, was cancelled or does not exist is blocked with the
        reason instead of waiting forever. That event does not start a turn: the dependency's own
        event already does, and a turn per block could ping-pong with a requeue."""
        db, now = self.p.db, time.time()
        for t in db.q("SELECT * FROM tasks WHERE status='running' AND NOT EXISTS "
                      "(SELECT 1 FROM runs WHERE runs.task=tasks.id AND runs.status='running')"):
            last = db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (t["id"],))
            if last and self._run_alive(last):
                continue
            with db.tx():
                db.update_task(t["id"], status="queued")
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (now, "daemon", "task_requeued", "low", f"#{t['id']} {t['title']} was marked running with no "
                      f"live run; queued again, no attempt spent", "handled", t["id"]))
            log(self.p, f"task {t['id']} requeued: marked running with no live run")
        # A cancel saves the task first and then asks its runs to stop; a crash in between must not
        # leave a cancelled task's run spending until its own limits end it.
        for r in db.q("SELECT runs.id, runs.dir FROM runs JOIN tasks ON tasks.id=runs.task "
                      "WHERE runs.status='running' AND tasks.status='cancelled'"):
            run_dir = self._run_dir(r)
            if run_dir.is_dir() and not (run_dir / "STOP").exists():
                try:
                    runner.request_stop(run_dir)
                    log(self.p, f"run {r['id']} of a cancelled task asked to stop")
                except OSError as e:
                    log(self.p, f"run {r['id']} of a cancelled task could not be asked to stop: {e}")
        for t, dep, why in db.dead_dependencies():
            reason = f"dependency #{dep} {why}" if dep is not None else "a dependency is not a task id"
            with db.tx():
                db.update_task(t["id"], status="blocked", blocked_reason=reason)
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (now, "daemon", "task_blocked", "normal", f"#{t['id']} {t['title']} is blocked: {reason}. "
                      f"The coordinator can re-point it with task_update depends_on (an empty list clears it), "
                      f"requeue it once the dependency is redone, or cancel it.", "handled", t["id"]))
        self._raise_dead_dependency_blocks(now)

    def _raise_dead_dependency_blocks(self, now: float) -> None:
        """A block on a dead dependency does not wake the coordinator, so one it let pass a whole
        turn could sit forever. It is raised once as an event that does."""
        db = self.p.db
        rows = db.q("SELECT * FROM tasks WHERE status='blocked' AND depends_on NOT IN ('', '[]')")
        for t in rows:
            dead = db.dead_dependency(dependency_ids(t))
            if not dead or not db.one("SELECT id FROM runs WHERE role='coordinator' AND status='ok' AND started>?",
                                      (t["updated"],)):
                continue
            dep, why = dead
            fp = f"dead-dependency:{t['id']}:{dep}"
            if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                continue
            redo = (f"re-add that work with task_add continues={dep} (its dependents move to the new task), "
                    if why in ("failed", "cancelled") else "")
            what = f"#{dep} ({why})" if dep is not None else "a dependency that is not a task id"
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", "dead_dependency", fp, "normal",
                  f"#{t['id']} {t['title']} is still blocked on {what} after a coordinator turn. Options: {redo}"
                  f"re-point #{t['id']} with task_update depends_on, or cancel it.", "queued", t["id"]))
            log(self.p, f"task {t['id']} still blocked on dead dependency {dep}; raised to the coordinator")

    def meter_running(self, every_s: float = 60) -> None:
        """Price runs still going from their stream, so status and the caps see a long run's spend
        before it ends. `finish_run` replaces the figure with the final one."""
        now = time.time()
        running = self.p.db.q("SELECT * FROM runs WHERE status='running'")
        self._metered = {k: v for k, v in self._metered.items() if k in {r["id"] for r in running}}
        for r in running:
            out = self._run_dir(r) / "output.jsonl"
            try:
                size = out.stat().st_size
            except OSError:
                continue
            seen = self._metered.get(r["id"])
            if seen and (seen[0] == size or now - seen[1] < every_s):
                continue
            self._metered[r["id"]] = (size, now)
            try:
                cost = self._priced(r, get_provider(r["provider"]).parse(out))
            except Exception:
                continue
            self.p.db.x("UPDATE runs SET cost_usd=?, cost_estimated=1 WHERE id=? AND status='running'",
                        (cost, r["id"]))

    def _priced(self, r: dict, usage) -> float:
        if usage.estimated and not usage.cost_usd:
            usage.cost_usd = bud.estimate_cost(self.p.db, self.cfg, r["provider"], r["model"] or "", {
                "input": usage.input_tokens, "output": usage.output_tokens,
                "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens})
        return usage.cost_usd

    def finish_run(self, r: dict, exit_info: dict) -> None:
        db, p = self.p.db, self.p
        run_dir = self._run_dir(r)
        runner.remove_private(run_dir)   # the runner removes them too, unless it died first
        prov = get_provider(r["provider"]).use(r["model"] or "", (self.cfg.get("pricing") or {}).get(r["provider"]))
        usage = prov.parse(run_dir / "output.jsonl", run_dir / "stderr.log")
        self._priced(r, usage)
        stopped = exit_info.get("stopped")
        # A resume that failed on its own and reported no tokens never got going, even if it printed
        # events: it costs nothing, so it ends as the free fallback to a fresh start (_finish_worker).
        failed_resume = not stopped and (exit_info.get("rc") != 0 or usage.error) and not _has_tokens(usage) \
            and _resume_never_started(json.loads(r["note"] or "{}"), usage)
        if usage.estimated and not usage.cost_usd and not failed_resume:
            usage.cost_usd = _cut_off_cost(run_dir, exit_info, usage, prov)
        status = "ok" if exit_info.get("rc") == 0 and not usage.error else "failed"
        if stopped in ("timeout", "budget", "stopped", "lost", "stalled", "shutdown", "resource_busy"):
            status = stopped if stopped != "stopped" else "killed"
        cut_off = None
        handed_off = r["role"] != "coordinator" and \
            (_read_result(run_dir / RESULT_FILE) or {}).get("status") in HANDOFF_STATES
        if status == "timeout" and handed_off:
            cut_off, status = status, "ok"   # it handed off before the clock ran out: the work is done, not wasted
        if usage.limited:
            status = "limit"
        if usage.auth_failed:
            status = "auth"
        note = json.loads(r["note"] or "{}")
        if usage.session_id:
            note["session_id"] = usage.session_id   # a run the host takes away resumes it (_resumable)
        # The runaway guard counts runs that ended without an outcome; a reboot, a host sleep or a
        # hand-off that stands is an outcome, not a loop.
        if status == "lost" and r["boot_id"] and r["boot_id"] != self.boot:
            note.update(not_waste="reboot", lost_to_reboot=self.boot, boot_at=self.boot_at)
        elif status in bud.WASTED and handed_off:
            note["not_waste"] = "handoff"
        elif status == "failed" and _resume_never_started(note, usage):
            note["not_waste"] = "resume"   # nothing ran: the task starts fresh (_finish_worker)
        elif status in SLEEP_CUT and self._slept_during(r, exit_info):
            # A run that overlapped a host sleep did not time out or fail on its own: the host went
            # away under it. It is lost to the sleep, like a run lost to a reboot.
            note.update(not_waste="sleep", lost_to_sleep=True, slept_s=exit_info.get("slept_s"))
            status = "lost"
        source = self._source_for(r)
        # Spend is booked at the run's end, not when the daemon gets to it: a run reaped after
        # downtime must not count toward the current hour. A stamp from the future is clamped.
        ended = min(float(exit_info.get("ended") or time.time()), time.time())
        # The run's end, its spend and what it did to its task commit together: a daemon stopped
        # half way leaves the run "running", and the next tick processes it again from disk.
        with db.tx():
            db.x("UPDATE runs SET ended=?, status=?, exit_code=?, cost_usd=?, cost_estimated=?, input_tokens=?, "
                 "output_tokens=?, cache_read_tokens=?, cache_write_tokens=?, note=? WHERE id=?",
                 (ended, status, exit_info.get("rc"), usage.cost_usd,
                  int(usage.estimated), usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                  usage.cache_write_tokens, json.dumps(note), r["id"]))
            db.spend(r["provider"], usage.cost_usd, source, account=r["account"] or "", estimated=usage.estimated,
                     tokens_in=usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
                     tokens_out=usage.output_tokens, ts=ended)
            if r["task"]:
                db.x("UPDATE tasks SET spent_usd=COALESCE(spent_usd,0)+? WHERE id=?", (usage.cost_usd, r["task"]))
            wins = usage.extra.get("windows")
            if wins and r["provider"] == "claude":
                bud.record_windows(db, as_windows(wins, r["account"] or ""))
            elif wins:
                bud.record_windows(db, [bud.Window(r["provider"], w["window"], w["utilization"], w.get("resets_at"),
                                                   r["account"] or "") for w in wins])
            if usage.limited:
                until = time.time() + 3600
                db.set_kv(f"limited:{r['provider']}", {"until": until, "note": usage.limit_note})
                self.alert(f"limit:{r['provider']}",
                           f"{r['provider']} refused work: {usage.limit_note}. Heavy work on it is paused for an "
                           f"hour; the account ({r['account'] or 'unknown'}) may need more credits or a higher cap.",
                           "high")
            if usage.auth_failed:
                # Logged out is not a task failure and not worth retrying blindly: pause this provider,
                # say exactly how to fix it, and probe again every 15 minutes (a cheap decision turn).
                db.set_kv(f"limited:{r['provider']}", {"until": time.time() + 900, "note": "logged out",
                                                       "creds": prov.credentials_stamp()})
                self.alert(f"auth:{r['provider']}",
                           f"{r['provider']} on {hostname()} is logged out ({(usage.final_text or usage.error)[:120]}). "
                           f"Log in once on that machine ({prov.login_hint}). "
                           f"Work resumes by itself; queued messages are kept.", "high", every_s=4 * 3600)
            if r["role"] == "coordinator":
                self._finish_coordinator(r, usage, status, note)
            else:
                self._finish_worker(r, usage, status, run_dir, cut_off if status == "ok" else None,
                                    rebooted=bool(note.get("lost_to_reboot")), slept=bool(note.get("lost_to_sleep")))
        log(p, f"run {r['id']} end status={status} cost=${usage.cost_usd:.3f}"
               f"{' (estimated)' if usage.estimated else ''} role={r['role']}")

    def _slept_during(self, r: dict, exit_info: dict) -> bool:
        """Whether the host slept while the run was going: its supervisor saw the wall clock run
        ahead of the monotonic one, or the daemon saw the host sleep between the run's start and end
        (a supervisor that died leaves no clock readings)."""
        if float(exit_info.get("slept_s") or 0) >= SLEPT_MIN_S:
            return True
        start = float(exit_info.get("started") or r["started"] or 0)
        # A lost run ended somewhere between its last sign of life and now.
        end = time.time() if exit_info.get("stopped") == "lost" else float(exit_info.get("ended") or time.time())
        return bool(start) and self._slept_between(start, end)

    def _source_for(self, r: dict) -> str:
        if r["role"] == "coordinator":
            return "coordinator"
        task = self.p.db.task(r["task"]) if r["task"] else None
        if task and task["origin"] == "schedule":
            labels = json.loads(task["labels"] or "[]")
            return f"schedule:{labels[0]}" if labels else f"task:{task['id']}"
        return f"task:{r['task']}" if r["task"] else r["role"]

    def _finish_coordinator(self, r: dict, usage, status: str, note: dict) -> None:
        db = self.p.db
        out = usage.structured if isinstance(usage.structured, dict) else last_json_object(usage.final_text or "")
        actions = (out or {}).get("actions")
        if (status == "lost" and not r["dir"]) or status == "shutdown" or note.get("lost_to_sleep"):
            # Never launched, ended by `ttp stop --kill`, or cut by a host sleep: not a failed turn.
            # Its messages and events stay queued for the next one.
            return
        if status == "auth":
            db.set_kv("coordinator_backoff_until", time.time() + 900)
            return
        if status != "ok" or not isinstance(actions, list):
            self._coordinator_failed(f"{status} {usage.error[:200]}")
            return
        db.set_kv("coordinator_failures", 0)
        default_chat = note.get("default_chat")
        problems = coord.apply(self.p, actions, default_chat=default_chat, user_turn=bool(note.get("messages")),
                               turn=r.get("id"))
        ids = note.get("messages", [])
        if ids:
            db.x(f"UPDATE messages SET handled=1 WHERE id IN ({','.join('?' * len(ids))})", ids)
        evs = note.get("events", [])
        if evs:
            db.x(f"UPDATE events SET status='handled' WHERE id IN ({','.join('?' * len(evs))})", evs)
        self._record_rejections([x[:500] for x in problems])
        db.set_kv("last_coordinator_summary", {"ts": time.time(), "summary": (out or {}).get("summary", "")})

    def _record_rejections(self, problems: list[str]) -> None:
        """Rejected actions reach the next turn's digest; they never start a turn by themselves.
        A rejection repeated on consecutive turns is a harness problem and is recorded once."""
        db = self.p.db
        before = set(db.kv(coord.REJECTED_KEY, []) or [])
        db.set_kv(coord.REJECTED_KEY, problems)
        if not problems:
            return
        db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
             (time.time(), "daemon", "rejected_actions", "normal", "; ".join(problems)[:1500], "handled"))
        seen = db.kv("rejected_repeats", []) or []
        for x in problems:
            key = hashlib.sha256(x.encode()).hexdigest()[:16]
            if x in before and key not in seen:
                seen.append(key)
                db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                     (time.time(), "harness", "rejected_repeat", "normal",
                      f"The coordinator repeated an action that was rejected: {x}", "handled"))
        db.set_kv("rejected_repeats", seen[-50:])

    def _coordinator_failed(self, why: str) -> None:
        db = self.p.db
        fails = int(db.kv("coordinator_failures", 0)) + 1
        db.set_kv("coordinator_failures", fails)
        db.set_kv("coordinator_backoff_until", time.time() + min(1800, 30 * 2 ** fails))
        if fails >= 3:
            self.alert("coordinator", f"The coordinator failed {fails} turns in a row (last: {why}). "
                       f"Messages are queued, not lost.", "high")

    def _finish_worker(self, r: dict, usage, status: str, run_dir: Path, ended: str | None = None,
                       rebooted: bool = False, slept: bool = False) -> None:
        db = self.p.db
        task = db.task(r["task"]) if r["task"] else None
        if not task:
            return
        handoff = _read_result(run_dir / RESULT_FILE)
        result = handoff or last_json_object(usage.final_text or "") or {}
        if task["status"] == "cancelled":
            # Cancelled while this run was ending: the decision stands. Keep what the run produced,
            # and tell the coordinator only if the work actually got done.
            summary = str((result.get("summary") if isinstance(result, dict) else "") or "")
            db.update_task(task["id"], result=dump_result({"summary": summary, "status": "cancelled",
                                                           "run_status": status}))
            if isinstance(result, dict) and result.get("status") == "done":
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), f"task:{task['id']}", "cancelled_but_done", "normal",
                      f"#{task['id']} {task['title']} was cancelled, but its run finished the work: "
                      f"{summary[:800]}", "queued", task["id"]))
            return
        if status == "lost" and not r["dir"] and not (run_dir / "lease").exists():
            # The daemon stopped between recording the run and launching it: nothing ran.
            with db.tx():
                db.update_task(task["id"], status="queued", blocked_reason=None)
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), "daemon", "task_requeued", "low", f"#{task['id']} {task['title']}: its run "
                      f"never started; queued again, no attempt spent", "handled", task["id"]))
            return
        # A hand-off written before the run ended badly (supervisor killed, reboot, timeout, stall)
        # stands: the work it reports is done and redoing it would repeat it.
        if status not in ("ok", "killed", "resource_busy") and (
                (handoff or {}).get("status") in HANDOFF_STATES
                or (status == "shutdown" and isinstance(result, dict) and result.get("status"))):
            ended, status = status, "ok"
        elif status == "shutdown":
            # The project was stopped, not the task: it resumes on the next start, on its own branch.
            db.update_task(task["id"], status="queued", blocked_reason="interrupted by `ttp stop --kill`; resumes")
            return
        if status == "failed" and _resume_never_started(json.loads(r["note"] or "{}"), usage) and not handoff:
            # The lost run's session could not be continued after all: nothing ran, so the task starts
            # fresh at once, no attempt spent. This run ended 'failed', so it is not resumed again.
            with db.tx():
                db.update_task(task["id"], status="queued", blocked_reason=None, not_before=None)
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), "daemon", "task_requeued", "low", f"#{task['id']} {task['title']}: its lost "
                      f"session could not be resumed; starts fresh, no attempt spent", "handled", task["id"]))
            return
        if status == "resource_busy":
            # The run lost the race for its resource and never started its agent: not an attempt.
            db.update_task(task["id"], status="queued", not_before=time.time() + 30,
                           blocked_reason="its resource stayed busy before the run could start; retries")
            return
        rstatus = result.get("status") if isinstance(result, dict) else None
        summary = str((result.get("summary") if isinstance(result, dict) else None)
                      or (usage.final_text or usage.error or "")[:1500])
        waiting = status == "ok" and rstatus == "waiting"
        # A host reboot is not the task's failure: no attempt, no delay, unless the task keeps being
        # the run the host went down under.
        reboot_lost = status == "lost" and rebooted
        # So is a host sleep, a few times: past max_reboot_losses since the task was last blocked it
        # counts an attempt again, so a task that fails on its own while the host also slept cannot
        # retry for free forever.
        if status == "lost" and slept:
            reboot_lost = self._reboot_losses(task["id"], "lost_to_sleep") <= int(
                self.cfg["budget"].get("max_reboot_losses", 3))
        no_handoff = status == "ok" and rstatus is None
        if waiting:
            new = "queued"   # a busy resource is not a failed attempt: the task comes back later
        elif no_handoff:
            # A clean exit without a hand-off is not evidence of done work: the worker may have
            # stopped mid-task ("I'll pick up later"). Count an attempt and retry with its last words.
            new, status = "failed", "no_handoff"
            summary = f"ended without a hand-off. Its last message: {summary}"[:1500]
        elif status == "ok" and rstatus in ("done", "blocked", "failed", "needs_review"):
            new = {"done": "done", "blocked": "blocked", "failed": "failed", "needs_review": "review"}[rstatus]
        elif status in ("limit", "auth") or reboot_lost:
            new = "queued"   # not an attempt: the account refused or the host went down, the task did not fail
        else:
            new = "failed"
        attempts = int(task["attempts"] or 0) + (0 if status in ("limit", "auth") or waiting or reboot_lost else 1)
        if new == "failed" and attempts < int(task["max_attempts"] or 3) and status in ("failed", "lost", "timeout",
                                                                                     "stalled", "no_handoff"):
            new = "queued"
        extra: dict = {}
        reason = None
        not_before = None
        if rebooted:
            extra["reboot"] = {"at": self.boot_at, "notes": _last_notes(run_dir)}
        wakes = _reboot_wakes(task)
        if reboot_lost and not slept:
            n = self._reboot_losses(task["id"]) + wakes
            if n >= int(self.cfg["budget"].get("max_reboot_losses", 3)):
                new, reason = "blocked", f"lost to a host reboot {n} times; it may be causing them"
        if waiting:
            # A cheap wake that found real work asks to go on at a higher tier: once per wake, at
            # once, and not as another wait, so the escalated run cannot escalate again.
            wake = json.loads(r["note"] or "{}").get("wake") or {}
            up = result.get("wake_tier")
            escalate = (wake.get("tier") in bud.TIER_ORDER and not wake.get("escalated")
                        and str(result.get("retry_after_s")) in ("0", "0.0") and up in bud.TIER_ORDER
                        and bud.TIER_ORDER.index(up) > bud.TIER_ORDER.index(wake["tier"]))
            try:
                waits = int(load_result(task["result"]).get("waits") or 0) + (0 if escalate else 1)
            except (TypeError, ValueError):
                waits = 1
            what = str(result.get("waiting_for") or summary)[:300]
            extra["waits"] = waits
            if escalate:
                not_before = time.time()
                extra["escalated_wake"] = True
                extra["woke"] = f"the {wake['tier']} wake found work and asked for {up}"
                reason = f"woke at {wake['tier']} and found work; runs again now at {up}"
            elif waits > int(self.cfg["budget"].get("max_waits", 24)):
                new, reason = "blocked", f"still waiting after {waits} tries: {what}"
            elif rebooted and result.get("survives_reboot") is not True and (
                    n := self._reboot_losses(task["id"]) + wakes) >= int(
                    self.cfg["budget"].get("max_reboot_losses", 3)):
                # This run is already one of the losses: its wait counts once, not again as a wake.
                new, reason = "blocked", f"lost to a host reboot {n} times; it may be causing them"
            elif rebooted and result.get("survives_reboot") is not True:
                # What it waited on died with the host: its next run finds out now, not at the timer.
                not_before = time.time()
                extra["woke"] = "the host rebooted"
                reason = f"waiting for {what}; the host rebooted, so it runs again now"
            else:
                not_before = time.time() + _retry_s(result)
                extra["waiting_since"] = time.time()
                reason = f"waiting for {what}; next try {time.strftime('%H:%M', time.localtime(not_before))}"
        if ended:
            extra["run_status"] = ended
        if wakes and new != "blocked":
            # Waits a reboot cut short count against max_reboot_losses; a block starts the count over.
            extra["reboot_wakes"] = wakes
        shown = rstatus or status
        if status in ("limit", "auth") and not rstatus and (prev := load_result(task["result"])).get("status") == "waiting":
            # A wake the account refused is the same wait: its retry stays a cheap wake.
            extra.update({k: prev[k] for k in WAIT_KEYS if k in prev})
            shown = "waiting"
        upd = {"status": new, "attempts": attempts, "result": dump_result(
            {"summary": summary, "status": shown, **extra,
             **({k: v for k, v in result.items()
                 if k not in ("summary", "waits", "waiting_since", "woke", "reboot_wakes", "escalated_wake")}
                if isinstance(result, dict) else {})})}
        if reason:
            upd["blocked_reason"] = reason[:500]
        elif new == "blocked":
            upd["blocked_reason"] = str(result.get("question") or result.get("blocked_reason") or summary)[:500]
        else:
            upd["blocked_reason"] = None
        if not_before:
            upd["not_before"] = not_before
        elif reboot_lost:
            upd["not_before"] = None
        elif new == "queued" and status not in ("limit", "auth"):
            upd["not_before"] = time.time() + 120 * attempts
        if isinstance(result, dict) and result.get("pr"):
            upd["pr_url"] = str(result["pr"])[:300]
        db.update_task(task["id"], **upd)
        if waiting and new == "queued":
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "task_waiting", "low",
                  f"#{task['id']} {task['title']}: {reason}", "handled", task["id"]))
            return
        if task["reply_chat"] and new in ("done", "failed", "blocked"):
            text = summary if new == "done" else f"(task #{task['id']} {new}) {summary}"
            need = upd.get("blocked_reason") if new == "blocked" else None
            if need and need not in summary:
                text += f"\nNeeds from you: {need}"
            chat = None if task["reply_chat"] == "all" else task["reply_chat"]
            db.post("out", text[:6000], chat=chat, kind="reply", severity="normal")
        sev = "high" if new == "blocked" else "normal"
        fups = result.get("followups") if isinstance(result, dict) else None
        fups = [f for f in fups if isinstance(f, dict) and f.get("title")] if isinstance(fups, list) else []
        # A plan's findings, plugin advice and follow-up specs are its product: each part gets its own
        # event, sized for the digest to show it whole, so an ordinary hand-off does not grow.
        where = _result_ref(self.p, run_dir if handoff is None else run_dir / RESULT_FILE)
        text = (f"#{task['id']} {task['title']} → {new} (run {ended or status}, {'~' if usage.estimated else ''}"
                f"${usage.cost_usd:.2f}): {_cut(summary, 1200, where)}")
        notes = ""
        if len(fups) > MAX_FOLLOWUPS:
            notes += (f"\nMore proposed follow-ups (their specs are in {where}): "
                      + "; ".join(str(f["title"])[:120] for f in fups[MAX_FOLLOWUPS:]))
        if isinstance(result, dict):
            facts = [f for f in (result.get("findings") or []) if isinstance(f, dict) and f.get("fact")]
            if facts:
                notes += "\nFindings (save the durable ones as memory):" + "".join(
                    f"\n- {f['fact']} [{f.get('source', '')}]" for f in facts)
            plugs = [x for x in (result.get("enable_plugins") or []) if isinstance(x, dict) and x.get("path")]
            if plugs:
                notes += "\nRecommended skill plugins for workers:" + "".join(
                    f"\n- {x['path']}: {x.get('why', '')}" for x in plugs)
        # A retry the daemon already scheduled, after a refusal (which has its own alert) or a run that
        # ended without a verdict, leaves nothing to decide: the final attempt's outcome starts the turn.
        # A timeout still does, since the task may need splitting before it times out again.
        quiet = new == "queued" and status in ("limit", "auth", "failed", "lost", "stalled", "no_handoff")
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), f"task:{task['id']}", f"task_{new}", sev, text, "handled" if quiet else "queued",
              task["id"]))
        if notes:
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "task_notes", "normal",
                  _cut(f"#{task['id']} {task['title']}:{notes}", coord.EVENT_CHARS_BY_KIND["task_notes"], where),
                  "handled" if quiet and len(fups) <= MAX_FOLLOWUPS else "queued", task["id"]))
        for f in fups[:MAX_FOLLOWUPS]:
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "followup_proposed", "normal",
                  f"proposed follow-up: {str(f['title'])[:200]} — "
                  f"{_cut(str(f.get('spec', '')), FOLLOWUP_SPEC_CHARS, where)}", "queued", task["id"]))

    # money ----------------------------------------------------------------------------------------
    def _refresh_meters(self, every_s: float = 600) -> None:
        """Providers that expose plan windows for free (no model call) are read in the background,
        so headroom stays current between runs. Claude's windows arrive with every run instead."""
        now = time.time()
        if now - getattr(self, "_last_meter", 0) < every_s or getattr(self, "_metering", False):
            return
        self._last_meter, self._metering = now, True
        wanted = {self.cfg.get("core_provider", "claude")} | {t["provider"] for t in self.p.db.q(
            "SELECT DISTINCT provider FROM tasks WHERE provider IS NOT NULL AND status IN ('queued','running')")}

        def work() -> None:
            from .db import DB
            db = DB(self.p.state / "project.db")        # this thread's own connection
            try:
                for name in wanted - {"claude", "fake"}:
                    try:
                        wins = get_provider(name).meter()
                    except Exception:
                        wins = []
                    if wins:
                        bud.record_windows(db, wins)
            finally:
                db.close()
                self._metering = False
        threading.Thread(target=work, daemon=True).start()

    def _provider_pause(self, prov: str) -> dict | None:
        """The provider's pause, or None once a logged-out pause is over because a login changed its
        credential files. The check is a stat, so a waiting pause costs no model call."""
        lim = self.p.db.kv(f"limited:{prov}")
        if not lim or lim.get("note") != "logged out" or not lim.get("creds") or lim.get("until", 0) <= time.time():
            return lim
        try:
            stamp = get_provider(prov).credentials_stamp()
        except Exception:   # an unknown provider keeps its pause until it expires
            return lim
        if not stamp or stamp == lim["creds"]:
            return lim
        self.p.db.set_kv(f"limited:{prov}", {**lim, "until": 0, "note": "credentials changed"})
        log(self.p, f"{prov} credentials changed; ending the logged-out pause")
        return None

    def update_gates(self) -> None:
        windows = bud.plan_windows(self.p.db)
        gates, news = {}, []
        # After a restart the last levels come from disk, so a change while the daemon was down is news.
        saved = {} if self.gates else (self.p.db.kv("gates") or {})
        for prov in {self.cfg.get("core_provider", "claude"), *[t["provider"] for t in self.p.db.q(
                "SELECT DISTINCT provider FROM tasks WHERE provider IS NOT NULL AND status IN ('queued','running')")]}:
            g = bud.evaluate(self.p.db, self.cfg, prov, windows)
            lim = self._provider_pause(prov)
            if lim and lim.get("until", 0) > time.time():
                bud._raise(g, "red", f"provider limit: {lim.get('note')}")
                g.max_parallel, g.allow_new_work, g.allow_optional = 0, False, False
            prev = self.gates.get(prov) or _saved_gate(saved.get(prov))
            provider_paused = any(r.startswith("provider limit") for r in g.reasons + (prev.reasons if prev else []))
            # On a plan, green and yellow are the pacing working as designed (more or fewer workers
            # as the account's burn moves); only nearing or hitting the limit is news to the user.
            pacing = (g.regime == "windows" and prev is not None
                      and {prev.level, g.level} <= {"green", "yellow"})
            if prev and prev.level != g.level and not provider_paused and not pacing:   # a pause has its own alert
                sev = "high" if g.level == "red" else "normal"
                capped = any("cap reached" in r for r in g.reasons)
                hint = ""
                if g.level == "red":
                    hint = ("New work is paused; replies to you continue. " +
                            ("You can raise the cap (carefully) by telling me, or in the web app."
                             if capped else "The web app's Budget tab shows what spent it."))
                news.append((f"Budget for {prov} is now {g.level}: {'; '.join(g.reasons) or 'back to normal'}. "
                               + hint, sev, f"budget:{prov}"))
            gates[prov] = g
        # One transaction: saved gates without their alert would hide the change from every later
        # tick and restart. Gates first: a relay reading an alert before the gates show red would
        # count it as cleared.
        with self.p.db.tx():
            self.p.db.set_kv("gates", {k: v.as_dict() for k, v in gates.items()})
            for text, sev, ref in news:
                self.p.db.post("out", text, chat=None, kind="alert", severity=sev, ref=ref)
        self.gates = gates

    # schedules and watchers ------------------------------------------------------------------------
    def run_schedules(self) -> None:
        db = self.p.db
        for s in sched.due(db):
            payload = json.loads(s["payload"] or "{}")
            try:
                if s["kind"] == "command":
                    status = self._run_command_watcher(s, payload)
                elif s["kind"] == "watcher":
                    from .watchers import run_builtin
                    status = run_builtin(self, s["name"], payload)
                else:
                    status = self._schedule_llm(s, payload)
            except Exception as e:
                status = f"error: {type(e).__name__}: {e}"[:200]
            sched.mark_ran(db, s, status)
            if sched.failing(status) and sched.failing(s["last_status"]):
                self._schedule_broken(s, status)
            self._progress()

    def _schedule_broken(self, s: dict, status: str) -> None:
        """Two failed runs in a row raise one alert; it clears itself on the next run that does not fail."""
        key = f"schedule:{s['name']}"
        if self.p.db.one("SELECT id FROM alerts WHERE key=? AND cleared IS NULL", (key,)):
            return
        why = ("it has no command to run; the coordinator sets one with schedule_set `command`"
               if status == "no command" else status)
        self.alert(key, f"Schedule {s['name']} failed twice in a row and does nothing until fixed: {why}", "high")

    def _run_command_watcher(self, s: dict, payload: dict) -> str:
        cmd = payload.get("command")
        if not cmd:
            return "no command"
        hours = payload.get("rewake_after_h", (self.cfg.get("screen") or {}).get("rewake_after_h", 6))
        try:
            rewake = float(hours) * 3600 if hours is not None else None
        except (TypeError, ValueError):
            rewake = 6 * 3600
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=str(self.p.root),
                                 timeout=_watcher_timeout(payload), env={**os.environ, "PATH": service_path()})
        except subprocess.TimeoutExpired:
            self.observe(f"watcher:{s['name']}", f"watcher command timed out: {cmd}", "normal",
                         rewake_after_s=rewake)
            return "timeout"
        text = (out.stdout or "").strip()
        if out.returncode not in (0, 1) and not text:
            text = f"watcher command failed rc={out.returncode}: {(out.stderr or '')[-500:]}"
        n = 0
        for obs in _observations(text):
            self.observe(f"watcher:{s['name']}", obs.get("text", ""), obs.get("severity"),
                         rewake_after_s=rewake, repeat=obs.get("repeat") is True)
            n += 1
        return f"ok ({n} observations)"

    def _schedule_llm(self, s: dict, payload: dict) -> str:
        db = self.p.db
        core = self.cfg.get("core_provider", "claude")
        gate = self.gates.get(core)
        if gate and not gate.allow_optional:
            return f"skipped: budget {gate.level}"
        if s["budget_usd_day"] is not None and sched.spent_today(db, s["name"]) >= float(s["budget_usd_day"]):
            return "skipped: daily budget used"
        if db.one("SELECT id FROM tasks WHERE origin='schedule' AND labels=? AND status NOT IN "
                  "('done','failed','cancelled')", (json.dumps([s["name"]]),)):
            return "skipped: previous run still open"
        # A review of a period with no work and no user message would report that nothing moved.
        # The built-in daily review opts in by name, so projects created before the flag get it too.
        if payload.get("skip_if_idle", s["name"] == "daily-review") and s["last_run"] and not self._active_since(
                float(s["last_run"]), s["name"]):
            return "skipped: nothing happened since the last run"
        spec = payload.get("spec") or s["description"]
        prompt_file = payload.get("prompt")
        if prompt_file and (self.p.harness / "prompts" / prompt_file).exists():
            spec = (self.p.harness / "prompts" / prompt_file).read_text() + "\n\n" + spec
        db.add_task(f"[{s['name']}] {s['description'][:120] or 'recurring task'}", spec, kind=payload.get("kind", "work"),
                    tier=payload.get("tier", "standard"), priority=int(payload.get("priority", 4)),
                    budget_usd=s["budget_usd_day"], origin="schedule", labels=[s["name"]])
        return "queued"

    def _active_since(self, since: float, schedule: str) -> bool:
        """Whether any worker ran, other than this schedule's own, or the user wrote, since `since`."""
        db = self.p.db
        return bool(db.one("SELECT id FROM messages WHERE direction='in' AND ts>?", (since,))
                    or db.one("SELECT runs.id FROM runs LEFT JOIN tasks ON tasks.id=runs.task "
                              "WHERE runs.role!='coordinator' AND runs.started>? AND NOT "
                              "(COALESCE(tasks.origin,'')='schedule' AND COALESCE(tasks.labels,'')=?)",
                              (since, json.dumps([schedule]))))

    def observe(self, source: str, text: str, hint: str | None = None, rewake_after_s: float | None = None,
                repeat: bool = False) -> None:
        if not text.strip():
            return
        again = {"rewake_after_s": rewake_after_s, "repeat": repeat}
        try:
            v = scr.screen(self.p.db, self.cfg, source, text, hint, jev=self.jev, **again)
        except JevOutOfFunds:
            self.alert("jev-funds", "The Jev account is out of credits. Screening falls back to rules "
                       "(more model calls, same coverage). Top up the Jev account to restore the savings.", "high")
            v = scr.screen(self.p.db, self.cfg, source, text, hint, jev=None, **again)
        if v.wake:
            self.p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                        (time.time(), source, "observation", v.fingerprint, v.severity, text[:4000], "queued"))

    # coordinator ------------------------------------------------------------------------------------
    def retry_rejected(self) -> None:
        """Wake the coordinator once a rejected action's blocking condition clears (the task cap's
        next free slot, or a raised cap), so work a turn could not start does not wait for an idle wake."""
        db = self.p.db
        wake = db.kv(coord.RETRY_WAKE_KEY) or {}
        if not wake.get("at"):
            return
        review = bool(wake.get("review"))
        later = coord.next_task_slot(db, coord.task_cap(self.cfg, review), review=review)
        with db.tx():
            if later is not None:
                # The slot is not free yet, or went to other work meanwhile: wait for the next one.
                if float(wake["at"]) <= time.time():
                    db.set_kv(coord.RETRY_WAKE_KEY, {**wake, "at": later})
                return
            db.x("DELETE FROM kv WHERE key=?", (coord.RETRY_WAKE_KEY,))
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (time.time(), "daemon", "retry_wake", "normal",
                  f"A slot is free for an action an earlier turn could not do ({wake.get('why', '')[:600]}). "
                  f"Do it now if it is still needed.", "queued"))

    def maybe_coordinate(self) -> None:
        db, c = self.p.db, self.cfg["coordinator"]
        if db.one("SELECT id FROM runs WHERE role='coordinator' AND status='running'"):
            return
        now = time.time()
        if now < float(db.kv("coordinator_backoff_until", 0)):
            return
        msgs = db.q("SELECT id, ts, chat FROM messages WHERE direction='in' AND handled=0 ORDER BY id LIMIT 30")
        evs = db.q("SELECT id, ts FROM events WHERE status='queued' ORDER BY id LIMIT ?",
                   (int(c.get("max_events_per_turn", 40)),))
        idle_due = False
        wake: dict = {}
        if not msgs and not evs:
            busy = db.one("SELECT id FROM tasks WHERE status IN ('queued','running')")
            last = float(db.kv("last_coordinator_turn", 0))
            gate = self.gates.get(self.cfg.get("core_provider", "claude"))
            if now - last <= min(float(c.get("idle_wake_s", 3600)), float(c.get("starve_wake_s", 300))):
                return
            # A wake turn that met the same state as the previous one had nothing new to decide:
            # each such repeat doubles the wait, up to a day. Explicit check-backs use schedules.
            fp = self._wake_fingerprint()
            prev = db.kv("idle_wake", {}) or {}
            repeats = int(prev.get("n", 0)) if prev.get("fp") == fp else 0
            backoff = min(float(c.get("idle_wake_s", 3600)) * 2 ** repeats, 86400.0) if repeats else 0.0
            idle_due = (not busy and now - last > max(float(c.get("idle_wake_s", 3600)), backoff)
                        and (gate is None or gate.allow_optional))
            starved = not idle_due and now - last > backoff and self._starved(gate, now - last)
            if not (idle_due or starved):
                return
            wake = {"fp": fp, "n": repeats + 1}
        else:
            newest = max([m["ts"] for m in msgs] + [e["ts"] for e in evs])
            oldest = min([m["ts"] for m in msgs] + [e["ts"] for e in evs])
            debounce = float(c.get("debounce_s", 15))
            if now - newest < debounce and now - oldest < 4 * debounce:
                return
        hour_turns = db.one("SELECT COUNT(*) n FROM runs WHERE role='coordinator' AND started>?", (now - 3600,))["n"]
        if hour_turns >= int(c.get("max_turns_per_hour", 30)) and not msgs:
            return
        gate = self.gates.get(self.cfg.get("core_provider", "claude"))
        if gate and gate.level == "red" and not msgs:
            return
        lim = self._provider_pause(self.cfg.get("core_provider", "claude"))
        if lim and lim.get("until", 0) > now:
            return   # provider paused (logged out or at its limit); the pause expiry is the retry
        gates = {k: v.as_dict() for k, v in self.gates.items()}
        default_chat = msgs[-1]["chat"] if msgs else None
        provider = self.cfg.get("core_provider", "claude")
        try:
            prompt = coord.digest(self.p, gates, [e["id"] for e in evs], [m["id"] for m in msgs])
            self.start_run("coordinator", prompt, provider, c.get("tier", "light"), str(self.p.base),
                           read_only=True, schema=coord.ACTIONS_SCHEMA, system=coord.system_prompt(self.p),
                           budget_usd=float(c.get("turn_budget_usd", 1.0)),
                           timeout_s=float(c.get("turn_timeout_s", 600)),
                           note={"messages": [m["id"] for m in msgs], "events": [e["id"] for e in evs],
                                 "default_chat": default_chat})
        except Exception as e:
            # A turn that cannot even start backs off like a failed turn instead of retrying every tick.
            log(self.p, "coordinator start failed: " + traceback.format_exc().replace("\n", " | ")[:2000])
            self._coordinator_failed(f"could not start: {type(e).__name__}: {e}"[:250])
            return
        db.set_kv("last_coordinator_turn", now)
        db.set_kv("idle_wake", wake)

    def _wake_fingerprint(self) -> str:
        """The state a wake turn decides on. Running counts as queued: dispatch moves tasks between
        the two without the coordinator. Spend numbers are left out; gate levels carry them."""
        db, p = self.p.db, self.p
        tasks = [(t["id"], "queued" if t["status"] == "running" else t["status"], t["priority"], t["depends_on"])
                 for t in db.q("SELECT id, status, priority, depends_on FROM tasks "
                               "WHERE status NOT IN ('done','failed','cancelled') ORDER BY id")]
        asks = [r["id"] for r in db.q("SELECT id FROM messages WHERE kind='ask' AND handled=0 ORDER BY id")]
        scheds = [(s["name"], s["enabled"], s["every_s"], s["at"])
                  for s in db.q("SELECT name, enabled, every_s, at FROM schedules ORDER BY name")]
        gates = sorted((k, g.level, g.allow_new_work) for k, g in self.gates.items())
        files = [p.charter_path, p.config_path, p.memory_index,
                 *(p.memory_dir.iterdir() if p.memory_dir.is_dir() else [])]
        mtimes = sorted((f.name, f.stat().st_mtime) for f in files if f.exists())
        paused = sorted(db.paused_resources())
        blob = json.dumps([tasks, asks, scheds, gates, mtimes] + ([paused] if paused else []), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def _starved(self, gate, since_last: float) -> bool:
        """Paid plan capacity sitting idle: worker slots are free, the plan is burning slower than
        its pace allows, and nothing is ready or about to be. Ask the coordinator for more
        independent work well before the idle wake would. Usage-billed work costs money whether or
        not it runs, so only plans qualify. A turn that adds no task doubles the wait for the next
        one, up to the idle wake; a new task resets it."""
        db, c = self.p.db, self.cfg["coordinator"]
        if gate is None or gate.regime != "windows" or gate.level != "green" or not gate.allow_new_work:
            return False
        if (gate.numbers.get("paced") or {}).get("until", 0) > time.time():
            return False    # a pace hold is the plan working as intended, not idle capacity
        if any(r.get("need_per_h") is None or (r.get("burn_per_h") is not None and r["burn_per_h"] >= r["need_per_h"])
               for r in gate.numbers.get("pace") or []):
            return False
        if self._free_slots(gate) <= 0 or self._dispatchable():
            return False
        # Queued work waiting on a retry timer, a dependency or a resource starts by itself; an open
        # question waits for the user.
        if db.one("SELECT id FROM tasks WHERE status='queued'") or \
                db.one("SELECT id FROM messages WHERE kind='ask' AND handled=0"):
            return False
        if coord.next_task_slot(db, coord.task_cap(self.cfg)) is not None:
            return False
        base = float(c.get("starve_wake_s", 300))
        newest = db.one("SELECT COALESCE(MAX(id),0) n FROM tasks")["n"]
        st = db.kv("starve") or {}
        wait = base if not st or newest > st.get("task", 0) else \
            min(float(st.get("wait", base)) * 2, float(c.get("idle_wake_s", 3600)))
        if since_last <= wait:
            return False
        db.set_kv("starve", {"task": newest, "wait": wait})
        return True

    # workers ----------------------------------------------------------------------------------------
    def dispatch(self) -> None:
        db = self.p.db
        running = db.q("SELECT provider, COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running' "
                       "GROUP BY provider")
        busy = {r["provider"]: r["n"] for r in running}
        ready = db.ready_tasks()
        self.check_disk()
        committed = None    # what running work under the dollar caps may still spend
        reached = set()     # tasks that got past the gates to the resource check
        paused = db.paused_resources()
        for task in ready:
            if self._disk_holds(task):
                continue
            # A paused resource holds its tasks in the queue, attempts untouched, until it resumes.
            hit = sorted(coord.task_resources(task) & paused.keys())
            note = task["blocked_reason"] or ""
            if hit:
                why = "; ".join(f"{r}: {paused[r]['reason']}" if paused[r].get("reason") else r for r in hit)
                held = (f"{PAUSED_NOTE} {why}; it starts once resumed "
                        f"(`ttp resume {self.p.name} --resource {hit[0]}`)")[:500]
                if note != held:
                    db.update_task(task["id"], blocked_reason=held)
                continue
            if note.startswith(PAUSED_NOTE):
                db.update_task(task["id"], blocked_reason=None)
            provider = task["provider"] or self.cfg.get("core_provider", "claude")
            gate = self.gates.get(provider) or bud.evaluate(db, self.cfg, provider, bud.plan_windows(db))
            if not gate.allow_new_work or busy.get(provider, 0) >= gate.max_parallel:
                continue
            if task["origin"] in ("schedule", "harness") and not gate.allow_optional:
                continue
            if bud.pace_hold(gate, task):
                continue
            remaining = (task["budget_usd"] or 0) - (task["spent_usd"] or 0)
            if task["budget_usd"] and remaining <= 0.05:
                db.update_task(task["id"], status="blocked", blocked_reason="task budget exhausted")
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), "daemon", "task_budget_exhausted", "normal",
                      f"#{task['id']} {task['title']} used its ${task['budget_usd']:.2f} budget", "queued", task["id"]))
                continue
            # Several runs start in one tick: under the dollar caps, each must fit in what is left
            # of them after what running work may still spend, or one tick commits past a cap.
            cost = max(remaining, 0.5) if task["budget_usd"] else 0.0
            if gate.regime == "caps":
                committed = self._committed_usd() if committed is None else committed
                if not self._fits_caps(gate, committed + cost):
                    if any(cap and cost > cap for cap in (gate.numbers.get("daily_cap"),
                                                          gate.numbers.get("weekly_cap"))):
                        db.update_task(task["id"], status="blocked",
                                       blocked_reason=f"its ${cost:.2f} budget is above the dollar cap")
                    continue
            # Checked last: a task that waits for its resource reserves it, and only a task that
            # would otherwise start now may hold others off the resource.
            reached.add(task["id"])
            if not self._resources_free(task, reserve=True):
                continue
            if task["kind"] == "review":
                task = self._size_review(task)
            wake = bud.wake_tier(task["tier"], load_result(task["result"]))
            tier = bud.clamp_tier(wake or task["tier"], gate)
            # Before _workdir_for, which would make a missing worktree afresh.
            lost = self._resumable(task, provider)
            try:
                cwd, branch = self._workdir_for(task)
            except Exception as e:
                db.update_task(task["id"], status="blocked", blocked_reason=f"workspace: {e}"[:400])
                self._unreserve(task)
                continue
            from .prompts import spec_digest, worker_resume, worker_system, worker_task
            try:
                system = worker_system(self.p)
                note = {"spec_sha": spec_digest(task)}
                if wake:
                    note["wake"] = {"tier": tier, "escalated": bool(load_result(task["result"]).get("escalated_wake"))}
                if lost and lost["cwd"] == cwd:
                    # The session holds the task and its own work: a short prompt continues it.
                    prompt = worker_resume(self.p, task, lost)
                    note["resumes"] = {"run": lost["run"], "session": lost["session"]}
                else:
                    prompt, lost = worker_task(self.p, task, cwd, branch, wake=note.get("wake")), None
                db.update_task(task["id"], status="running", branch=branch, blocked_reason=None)
                self.start_run("worker" if task["kind"] != "review" else "reviewer", prompt, provider, tier, cwd,
                               task=task, budget_usd=max(remaining, 0.5) if task["budget_usd"] else None,
                               read_only=False, append_system=system, note=note,
                               resume=lost["session"] if lost else None)
            except Exception as e:
                self._start_failed(task, e)
                self._unreserve(task)
                continue
            self._start_failures = 0
            self._progress()   # each start may have added a worktree
            busy[provider] = busy.get(provider, 0) + 1
            if gate.regime == "caps":
                committed += cost
        # A task a gate kept out this tick cannot start, so it must not hold `ttp lock` commands off.
        for task in ready:
            if task["id"] not in reached:
                self._unreserve(task)

    def _resumable(self, task: dict, provider: str) -> dict | None:
        """The task's last run, when the host took it away (reboot, sleep, lost supervisor) after
        real progress and its agent session can be continued in the same working directory: a fresh
        start would pay again for everything that run read and did. None otherwise: failed,
        timed-out and finished runs never resume, nor does a run whose hand-off stands."""
        want = self.cfg["budget"].get("resume_lost")
        if not isinstance(want, dict):
            return None
        r = self.p.db.one("SELECT * FROM runs WHERE task=? AND role!='coordinator' ORDER BY id DESC LIMIT 1",
                          (task["id"],))
        if not r or r["status"] != "lost" or r["provider"] != provider or not r["dir"]:
            return None
        note = json.loads(r["note"] or "{}")
        session = note.get("session_id")
        run_dir = Path(r["dir"])
        if not session or (_read_result(run_dir / RESULT_FILE) or {}).get("status") in HANDOFF_STATES:
            return None
        took = float(r["ended"] or 0) - float(r["started"] or 0)
        if float(r["cost_usd"] or 0) < float(want.get("min_usd", 0.5)) and took < float(want.get("min_s", 600)):
            return None
        spec = _read_result(run_dir / "run.json") or {}
        cwd, run_env = str(spec.get("cwd") or ""), spec.get("env") if isinstance(spec.get("env"), dict) else {}
        prov = get_provider(provider)
        if not cwd or not Path(cwd).is_dir() or not prov.resume_args(session) or \
                not prov.session_saved(session, cwd, run_env):
            return None
        cause = "reboot" if note.get("lost_to_reboot") else "sleep" if note.get("lost_to_sleep") else "lost"
        return {"run": r["id"], "session": session, "cwd": cwd, "dir": str(run_dir), "ended": r["ended"],
                "cause": cause, "spec_sha": note.get("spec_sha")}

    def _unreserve(self, task: dict) -> None:
        for res in _exclusive(task):
            locks.unreserve(locks.reserve_path(self.p.state / "locks", res), f"task #{task['id']}")

    def _committed_usd(self) -> float:
        """Budget that running workers on providers under the dollar caps have left to spend."""
        rows = self.p.db.q("SELECT t.provider, t.budget_usd, t.spent_usd FROM runs r JOIN tasks t ON t.id=r.task "
                           "WHERE r.status='running' AND r.role!='coordinator'")
        core = self.cfg.get("core_provider", "claude")
        return sum(max((r["budget_usd"] or 0) - (r["spent_usd"] or 0), 0) for r in rows
                   if getattr(self.gates.get(r["provider"] or core), "regime", "caps") == "caps")

    @staticmethod
    def _fits_caps(gate, usd: float) -> bool:
        n = gate.numbers
        return all(not cap or spent + usd <= cap for spent, cap in (
            (n.get("spent_24h", 0), n.get("daily_cap")), (n.get("spent_7d", 0), n.get("weekly_cap"))))

    def check_disk(self) -> None:
        """The disk guard. Free space under the project folder and its worktrees below the smaller of
        `disk.min_free_pct` of the disk and `disk.min_free_gb` (either at 0 turns it off) holds new
        tasks that may build or check out code; see _disk_holds. One high alert per episode, which ends
        once free space is back above DISK_RESUME times the threshold, so a disk hovering at the
        line does not flap. The episode is kept in the database: a restart neither re-alerts nor forgets it."""
        cfg = self.cfg.get("disk", {})
        pct, gb = float(cfg.get("min_free_pct", 5) or 0), float(cfg.get("min_free_gb", 150) or 0)
        worst = None   # (margin, path, free, total, threshold)
        for path in {self.p.base.resolve(), self.p.worktrees.resolve()}:
            try:
                u = shutil.disk_usage(path)
            except OSError:
                continue
            need = min(pct / 100 * u.total, gb * 1e9)
            margin = u.free - need * (DISK_RESUME if self._disk_low else 1)
            if worst is None or margin < worst[0]:
                worst = (margin, path, u.free, u.total, need)
        if worst is None:
            return
        _, path, free, total, need = worst
        self._disk_free = free
        low = need > 0 and worst[0] < 0
        now = time.time()
        db = self.p.db
        info = {"path": str(path), "free_gb": round(free / 1e9, 1), "total_gb": round(total / 1e9, 1),
                "threshold_gb": round(need / 1e9, 1), "resume_gb": round(need * DISK_RESUME / 1e9, 1), "low": low,
                "checked": now}
        last = db.kv("disk") or {}
        if (low != last.get("low") or abs(info["free_gb"] - float(last.get("free_gb") or 0)) >= 1
                or now - float(last.get("checked") or 0) > 600):
            db.set_kv("disk", info)
        if low == self._disk_low:
            return
        self._disk_low = low
        if low:
            db.set_kv("disk_low", {"path": str(path), "free_gb": info["free_gb"], "threshold_gb": info["threshold_gb"],
                                   "since": now})
            log(self.p, f"disk low: {free / 1e9:.1f} GB free under {path} (guard {need / 1e9:.1f} GB); "
                        f"only questions and plans start")
            self.alert("disk", f"Only {free / 1e9:.1f} GB free under {path} (guard: {need / 1e9:.0f} GB, the smaller "
                               f"of {pct:g}% of the disk and {gb:g} GB). New tasks other than questions and plans "
                               f"are held until {need * DISK_RESUME / 1e9:.0f} GB are free; running work, questions, plans "
                               f"and replies continue. Finished tasks' worktrees are removed as they end; "
                               f"`ttp prune {self.p.name}` sweeps now and lists the ones kept.", "high", every_s=0)
        else:
            db.set_kv("disk_low", None)
            log(self.p, f"disk space ok again: {free / 1e9:.1f} GB free under {path}")

    def check_release(self) -> None:
        """Hourly (and at start): is a newer tt-project installed than this harness runs? Status and
        the web app say so while it is. With upgrade.auto on, and no push or upgrade in flight, start
        this project's own `ttp upgrade` once per release; it restarts this daemon, keeping workers."""
        now = time.time()
        if now < self._release_due:
            return
        self._release_due = now + release.CHECK_S
        db = self.p.db
        try:
            d = release.drift(self.p)
        except Exception as e:   # a half-written install must not stop the tick
            log(self.p, f"release check failed: {type(e).__name__}: {e}")
            return
        if d != db.kv(release.KV_RELEASE):
            db.set_kv(release.KV_RELEASE, d)
            if d:
                log(self.p, f"tt-project {d['installed']} installed; harness on {d['current']}")
        if not d or not (self.cfg.get("upgrade") or {}).get("auto", True) or db.kv("paused", False):
            return
        why = release.hold_reason(self.p, d)
        if why:
            if why.endswith("in flight"):
                self._release_due = now + release.HELD_RECHECK_S
            if (db.kv(release.KV_AUTO) or {}).get("key") != d["key"] or why.endswith("in flight"):
                log(self.p, f"automatic upgrade to {d['installed']} held: {why}")
            return
        log(self.p, f"automatic upgrade from {d['current']} to {d['installed']}: starting `ttp upgrade`")
        try:
            release.start(self.p, d)
        except Exception as e:
            release.finish(self.p, "failed", why=f"{type(e).__name__}: {str(e)[:200]}")
            log(self.p, f"automatic upgrade did not start: {type(e).__name__}: {e}")

    def check_resource_trouble(self, every_s: float = 60) -> None:
        """A resource whose tasks keep failing (machines.trouble) starts a coordinator turn once per
        episode, so it moves the open work on it to a healthy alternative instead of retrying on it.
        Waits alone never start one: a busy resource is not a broken one. The episode ends only once
        the resource has had no failure for 24 h, so a count hovering at the threshold does not
        start a new one each time; it is kept in the database across restarts."""
        now = time.time()
        if now - self._trouble_checked < every_s:
            return
        self._trouble_checked = now
        db = self.p.db
        try:
            seen = machines.stats(db, now)
            bad = machines.trouble(db, now, seen)
            known = machines.load()
        except Exception:
            log(self.p, "resource trouble check: " + traceback.format_exc().replace("\n", " | ")[:1000])
            return
        told = db.kv("resource_trouble") or {}
        new = [n for n in sorted(bad) if n not in told and bad[n]["failures"] >= machines.TROUBLE_AT
               and bad[n].get("tasks")]
        with db.tx():
            for name in new:
                text = machines.trouble_line(name, bad[name], known, set(bad) | set(db.paused_resources()))
                db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                     (now, "daemon", "resource_trouble", "normal",
                      f"Resource {text}. Route around it: move its tasks to a healthy machine the charter "
                      f"allows (task_update `resources`), record the decision and notify the user; ask "
                      f"(blocking access) only if the charter allows no alternative.", "queued"))
            live = {n: told.get(n, now) for n in (*told, *new) if (seen.get(n) or {}).get("failures")}
            if live != told:
                db.set_kv("resource_trouble", live)

    def _disk_holds(self, task: dict) -> bool:
        """Under the disk guard, tasks that may check out, build or test code (all but questions and
        plans) wait in the queue, attempts untouched. Questions and plans still run, so the coordinator can look into it, but not when
        the disk is nearly full: that would corrupt state and fail runs half way."""
        if not self._disk_low:
            return False
        return task["kind"] not in DISK_LIGHT_KINDS or (self._disk_free or 0) < DISK_FLOOR_GB * 1e9

    def prune_worktrees(self, every_s: float = 300) -> None:
        """Tidy finished tasks' worktrees (worktree.sweep) soon after they end: every `every_s`, and
        at once when a task finished since the last sweep. `disk.worktree_retention_days` delays
        removal (null, the default: no delay beyond worktree.held_by; 0 or less: never tidy, as
        before). A worktree kept for a reason is checked again when its task changes or after
        KEEP_RECHECK_S. Branches are never deleted."""
        now = time.time()
        cfg = self.cfg.get("disk", {})
        days = cfg.get("worktree_retention_days")
        if days is not None and float(days) <= 0:
            return
        latest = self.p.db.one("SELECT MAX(updated) m FROM tasks WHERE status IN (%s)"
                               % ",".join("?" * len(TERMINAL_TASK_STATES)), TERMINAL_TASK_STATES)["m"] or 0
        if (now - self._last_prune < every_s and latest <= self._pruned_upto) or not self.p.worktrees.is_dir():
            return
        self._last_prune, self._pruned_upto = now, latest
        days = float(days or 0)

        def recent(task: dict) -> bool:
            memo = self._kept.get(task["id"])
            return bool(memo) and memo[0] == task["updated"] and now - memo[1] < KEEP_RECHECK_S
        for r in worktree.sweep(self.p, older_than_s=days * 86400, names=cfg.get("cache_dirs"), skip=recent):
            if r["cleared"]:
                log(self.p, f"worktree {r['path']} of task {r['task']}: removed {', '.join(r['cleared'][:10])}")
            if r["why"] is None:
                self._kept.pop(r["task"], None)
                log(self.p, f"worktree {r['path']} of task {r['task']} ({r['status']}) removed; "
                            f"branch {r['branch'] or '?'} kept")
            else:
                if r["task"] not in self._kept:
                    log(self.p, f"worktree {r['path']} of task {r['task']} kept: {r['why']}")
                # Held (see worktree.held_by): checked again every sweep (no git work), so it goes soon after the hold ends.
                self._kept[r["task"]] = (r["updated"], 0 if r.get("held") else now, r["why"])
        kept = {str(t): why for t, (_, _, why) in sorted(self._kept.items())
                if (self.p.worktrees / f"t{t}").exists()}
        if kept != (self.p.db.kv("worktrees_kept") or {}):
            self.p.db.set_kv("worktrees_kept", kept or None)

    def probe_waiting(self) -> None:
        """A waiting task may name a shell probe (`retry_when`) for the thing it waits on. The probe
        runs here, model-free and in the background. Exit 0 makes the task due at once. When its
        `retry_after_s` timer runs out while the probe still exits 1 ("not yet"), the task sleeps
        another `retry_after_s` instead of spending a worker run to find that out. A broken probe
        (any other exit, a timeout, a probe that cannot start) wakes it at its timer so a worker can
        fix the probe, and `waiting.max_hold_s` after the hand-off it wakes whatever the probe says."""
        db, now = self.p.db, time.time()
        for tid, (proc, started) in list(self._probes.items()):
            rc = proc.poll()
            if rc is None and now - started < PROBE_TIMEOUT_S:
                continue
            del self._probes[tid]
            if rc is None:
                _kill_group(proc)
            self._probe_rc[tid] = ("timeout" if rc is None else rc, now)
            if rc == 0:
                task = db.task(tid)
                if task and task["status"] == "queued" and (task["not_before"] or 0) > now:
                    self._wake_waiting(task, "probe passed", now)
        for t in db.q("SELECT * FROM tasks WHERE status='queued' AND not_before IS NOT NULL"):
            prev = load_result(t["result"])
            probe = prev.get("retry_when")
            if prev.get("status") != "waiting" or not isinstance(probe, str) or not probe.strip():
                continue
            if t["not_before"] <= now:
                # Hand-offs from before the hold, and tasks already woken, keep their timer.
                if prev.get("woke") or not isinstance(prev.get("waiting_since"), (int, float)):
                    continue
                if not self._hold_waiting(t, prev, now):
                    continue
            elif t["id"] in self._probes or now - self._probed.get(t["id"], 0) < PROBE_EVERY_S:
                continue
            self._start_probe(t["id"], probe, now)

    def wake_after_reboot(self) -> None:
        """Once per daemon start: a waiting task that handed off before this host booted waits on
        something the reboot may have ended (a detached job, a /tmp file, device state). It is due
        now, past its timer and its probe, unless its hand-off said `survives_reboot`."""
        if self._boot_woken:
            return
        self._boot_woken = True
        if not self.boot_at:
            return
        db = self.p.db
        for t in db.q("SELECT * FROM tasks WHERE status='queued' AND not_before IS NOT NULL"):
            prev = load_result(t["result"])
            if prev.get("status") != "waiting" or prev.get("survives_reboot") is True:
                continue
            since = prev.get("waiting_since")
            since = since if isinstance(since, (int, float)) else t["updated"]
            if not since or since >= self.boot_at:
                continue
            self._probe_rc.pop(t["id"], None)
            wakes = _reboot_wakes(t) + 1
            n = self._reboot_losses(t["id"]) + wakes
            if n >= int(self.cfg["budget"].get("max_reboot_losses", 3)):
                # Its detached job may be what takes the host down: stop waking it.
                prev.pop("reboot_wakes", None)
                reason = f"lost to a host reboot {n} times; it may be causing them"
                with db.tx():
                    db.update_task(t["id"], status="blocked", blocked_reason=reason, result=dump_result(prev))
                    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                         (time.time(), f"task:{t['id']}", "task_blocked", "high",
                          f"#{t['id']} {t['title']} → blocked: {reason}", "queued", t["id"]))
                log(self.p, f"task {t['id']} blocked: {reason}")
                continue
            db.update_task(t["id"], not_before=None, result=dump_result(
                {**prev, "woke": "the host rebooted", "reboot": {"at": self.boot_at}, "reboot_wakes": wakes}))
            log(self.p, f"task {t['id']} waited from before the reboot; due now")

    def _reboot_losses(self, tid: int, key: str = "lost_to_reboot") -> int:
        """Runs of the task lost to a host reboot (or, by key, a host sleep) since it was last
        blocked: a person or the coordinator who requeues a task blocked for reboots starts its count
        over."""
        db = self.p.db
        since = (db.one("SELECT MAX(ts) ts FROM events WHERE task=? AND kind='task_blocked'", (tid,)) or {}).get("ts")
        return sum(1 for x in db.q("SELECT note FROM runs WHERE task=? AND status='lost' AND note LIKE ? "
                                   "AND started>?", (tid, f"%{key}%", since or 0))
                   if json.loads(x["note"] or "{}").get(key))

    def _start_probe(self, tid: int, probe: str, now: float) -> None:
        self._probed[tid] = now
        try:
            proc = subprocess.Popen(probe, shell=True, cwd=str(self.p.root), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except OSError as e:
            log(self.p, f"task {tid} retry_when probe could not start: {e}")
            self._probe_rc[tid] = ("could not start", now)
            return
        self._probes[tid] = (proc, now)

    def _hold_waiting(self, task: dict, prev: dict, now: float) -> bool:
        """A waiting task whose timer ran out: wake it, or put it back to sleep while its probe says
        "not yet". True when a probe must run before that can be decided."""
        tid = task["id"]
        max_hold = float((self.cfg.get("waiting") or {}).get("max_hold_s") or 6 * 3600)
        since = float(prev["waiting_since"])
        rc, at = self._probe_rc.get(tid, (None, 0.0))
        fresh = at >= since and now - at <= 2 * PROBE_EVERY_S
        if fresh and rc == 0:
            self._wake_waiting(task, "probe passed", now)
        elif fresh and rc != 1:
            self._wake_waiting(task, f"probe broken: {rc if isinstance(rc, str) else f'exit {rc}'}", now)
        elif now >= since + max_hold:
            self._wake_waiting(task, f"held {max_hold / 3600:g} h, probe still failing", now)
        elif fresh:
            nb = min(now + _retry_s(prev), since + max_hold)
            what = str(prev.get("waiting_for") or prev.get("summary") or "")[:300]
            db = self.p.db
            db.update_task(tid, not_before=nb, blocked_reason=(
                f"waiting for {what}; its probe says not yet; next try "
                f"{time.strftime('%H:%M', time.localtime(nb))}")[:500])
            log(self.p, f"task {tid} retry_when probe still failing; asleep until "
                        f"{time.strftime('%H:%M', time.localtime(nb))}")
        else:
            # No recent verdict (the daemon restarted): ask the probe before waking a worker.
            self.p.db.update_task(tid, not_before=now + PROBE_TIMEOUT_S)
            return tid not in self._probes
        return False

    def _wake_waiting(self, task: dict, why: str, now: float) -> None:
        """Make a waiting task due now and tell its next run why it woke."""
        tid = task["id"]
        self._probe_rc.pop(tid, None)
        prev = load_result(task["result"])
        self.p.db.update_task(tid, not_before=min(task["not_before"] or now, now),
                              result=dump_result({**prev, "woke": why}))
        gate = self.gates.get(task["provider"] or self.cfg.get("core_provider", "claude"))
        held = gate and (not gate.allow_new_work or (task["origin"] in ("schedule", "harness")
                                                      and not gate.allow_optional))
        log(self.p, f"task {tid} retry_when {why}; {f'held by gate {gate.level}' if held else 'dispatching'}")

    def _start_failed(self, task: dict, e: Exception) -> None:
        """Nothing was launched, so no attempt is spent. The task waits a minute before the next try,
        so a lasting cause (a full disk, a broken install) cannot spin; three failures in a row alert."""
        db = self.p.db
        why = f"could not start a run: {type(e).__name__}: {e}"[:400]
        log(self.p, f"task {task['id']} {why}")
        with db.tx():
            db.update_task(task["id"], status="queued", not_before=time.time() + 60, blocked_reason=why)
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), "daemon", "run_start_failed", "low", f"#{task['id']} {task['title']}: {why}",
                  "handled", task["id"]))
        self._start_failures += 1
        if self._start_failures >= 3:
            self.alert("run-start", f"Runs cannot start ({why}). Tasks stay queued and retry every minute.", "high")

    def _resources_free(self, task: dict, reserve: bool = False) -> bool:
        """Only tasks labelled `exclusive:<name>` hold a resource for their whole run; they share
        its slot count (config `resources`, default 1). A `resource:<name>` label means the task uses
        the resource for some commands: those take the resource's lock (`ttp lock`) or its own queue,
        so the rest of the task runs in parallel with other work instead of waiting for the slot.
        With reserve, a task kept out only by `ttp lock` commands reserves the resource so new ones
        wait; the reservation lapses unless the next dispatch refreshes it.

        At most twice its slots run at once among the tasks that use a resource either way: more
        would only queue in `ttp lock` on a worker slot and a wall clock that other work could use."""
        if coord.task_resources(task) & self.p.db.paused_resources().keys():
            return False
        limits = self.cfg.get("resources", {})
        for res in _shared(task):
            users = self.p.db.one("SELECT COUNT(*) n FROM tasks WHERE status='running' AND (labels LIKE ? "
                                  "OR labels LIKE ?)", (f'%"resource:{res}"%', f'%"exclusive:{res}"%'))["n"]
            if users >= 2 * max(int(limits.get(res, 1) or 1), 1):
                return False
        for res in _exclusive(task):
            limit = int(limits.get(res, 1))
            # Running exclusive tasks count even before their supervisor has taken its slot; the
            # lock files show the slots `ttp lock` commands hold.
            busy = self.p.db.one("SELECT COUNT(*) n FROM tasks WHERE status='running' AND labels LIKE ?",
                                 (f'%"exclusive:{res}"%',))["n"]
            if busy >= limit:
                return False
            if not locks.any_free(self._slot_paths(res)):
                if reserve:
                    locks.reserve(locks.reserve_path(self.p.state / "locks", res), f"task #{task['id']}")
                return False
        return True

    def _slot_paths(self, res: str) -> list[Path]:
        return locks.slot_paths(self.p.state / "locks", res, int(self.cfg.get("resources", {}).get(res, 1) or 1))

    def _free_slots(self, gate) -> int:
        running = self.p.db.one("SELECT COUNT(*) n FROM runs WHERE provider=? AND status='running' "
                                "AND role!='coordinator'", (gate.provider,))["n"]
        return max(int(gate.max_parallel) - running, 0)

    def _dispatchable(self) -> bool:
        """Whether any queued task could start now (dependencies done, resources free)."""
        return any(self._resources_free(t) for t in self.p.db.ready_tasks())

    def _size_review(self, task: dict) -> dict:
        """A review runs at the tier its diff needs, not the one it was queued with. Deep stays the
        coordinator's call. A retry never drops to light: the light try may be why it
        failed. A diff that cannot be measured keeps the tier the task was given."""
        if task["tier"] == "deep":
            return task
        try:
            refs = worktree.reviewed_refs(self.p, task)
            since = self._reviewed_heads(task)
            # A re-review is sized by the fix since the blocked review; nothing new, the whole stack.
            changes = (since and worktree.diff_lines(self.p, refs, since)) or worktree.diff_lines(self.p, refs)
        except Exception as e:
            log(self.p, f"task {task['id']}: review diff not measured: {e}")
            return task
        if changes is None:
            return task
        tier = bud.review_tier(changes, self.cfg)
        if tier == "light" and task["attempts"]:
            tier = "standard"
        if tier != task["tier"]:
            log(self.p, f"task {task['id']}: review tier {task['tier']} -> {tier} "
                        f"({len(changes)} files, {sum(n or 0 for n in changes.values())} lines)")
            self.p.db.update_task(task["id"], tier=tier)
        return dict(task, tier=tier)

    def _reviewed_heads(self, task: dict) -> list[str]:
        """Heads earlier reviews of this stack saw (their `metrics.reviewed_head`): reviews the task
        continues, directly or through the fix it depends on (a `continues:` chain)."""
        heads, seen = [], {task["id"]}
        todo = [continues_id(task)] + [continues_id(t) for i in dependency_ids(task) if i is not None
                                       for t in [self.p.db.task(i)] if t]
        while todo:
            tid = todo.pop()
            t = self.p.db.task(tid) if tid is not None and tid not in seen else None
            if not t:
                continue
            seen.add(tid)
            metrics = load_result(t["result"]).get("metrics") if t["kind"] == "review" else None
            head = metrics.get("reviewed_head") if isinstance(metrics, dict) else None
            if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{7,40}", head.strip()):
                heads.append(head.strip())
            todo.append(continues_id(t))
        return heads

    def _workdir_for(self, task: dict) -> tuple[str, str | None]:
        if task["kind"] == "harness":
            return str(self.p.harness), None
        if task["kind"] == "code" and worktree.is_git(self.p.root):
            path, branch = worktree.ensure(self.p, task)
            return str(path), branch
        return str(self.p.root), None

    # notifications and Slack -------------------------------------------------------------------------
    def alert(self, key: str, text: str, severity: str = "high", every_s: float = 6 * 3600) -> None:
        """Deduplicated broadcast: the same condition alerts at most once per `every_s`, across
        daemon restarts too (an upgrade must not re-announce a condition the user already has).
        The key is kept as the message's ref and, for a high alert, opens an episode that clears
        itself once the condition does (alerts.sweep); the same condition may then alert again."""
        now = time.time()
        db = self.p.db
        with db.tx():   # marked sent only together with the message
            sent = db.kv("alerts_sent", {})
            if now - float(sent.get(key, 0)) < every_s:
                return
            sent[key] = now
            db.set_kv("alerts_sent", {k: v for k, v in sent.items() if now - float(v) < 7 * 86400})
            db.post("out", text, chat=None, kind="alert", severity=severity, ref=key)

    def sweep_alerts(self) -> None:
        """Close alert episodes whose condition cleared (stored with the time; the chats hear it once)."""
        for ep in alerts.sweep(self.p.db):
            log(self.p, f"alert cleared: {ep['key']} ({ep['cleared_why']})")

    def slack(self):
        if not self.cfg["notify"].get("slack"):
            return None
        if self._slack is None:
            sec = load_secrets().get("slack") or {}
            if not sec.get("bot_token"):
                return None
            from .slack import Slack
            self._slack = Slack(sec["bot_token"], sec.get("user_id"), sec.get("user_email"))
        return self._slack

    def deliver_outbound(self) -> None:
        sl = self.slack()
        if not sl:
            return
        from .web import cleared
        db = self.p.db
        floor = SEVERITY_RANK.get(self.cfg["notify"].get("slack_min_severity", "high"), 2)
        last = int(db.kv("slack_last_out", 0))
        rows = db.q("SELECT * FROM messages WHERE direction='out' AND id>? ORDER BY id LIMIT 20", (last,))
        for m in rows:
            to_slack = ((m["chat"] is None and SEVERITY_RANK.get(m["severity"], 1) >= floor and m["kind"] != "info"
                         and not cleared(db, m, time.time())) or m["chat"] == "slack")
            if to_slack:
                try:
                    thread = m["ref"] if m["chat"] == "slack" else None
                    ts = sl.post(self.p.name, m["text"], thread_ts=thread)
                    threads = set(db.kv("slack_threads", []))
                    threads.add(ts)
                    db.set_kv("slack_threads", sorted(threads)[-500:])
                except Exception as e:
                    log(self.p, f"slack post failed: {e}")
                    if not sl.rejected_message(e):
                        return
                    tries = self._slack_rejects[m["id"]] = self._slack_rejects.get(m["id"], 0) + 1
                    if tries < 3:
                        return
                    log(self.p, f"slack skipped message {m['id']} after {tries} rejections: {e}")
                self._slack_rejects.pop(m["id"], None)
            db.set_kv("slack_last_out", m["id"])

    def poll_slack(self) -> None:
        sl = self.slack()
        now = time.time()
        if not sl or now - self._last_slack < float(self.cfg["notify"].get("slack_poll_s", 20)):
            return
        self._last_slack = now
        from .slack import THREAD_SCAN_S, THREAD_WINDOW_S, projects_in_dm, route
        db = self.p.db
        oldest = str(db.kv("slack_oldest", f"{now - 60:.6f}"))
        # Each thread has its own read position; `floor` is where a thread not read yet starts: the
        # cursor when all recent posts were last checked, so a reply is not skipped for a newer message.
        replies = db.kv("slack_replies") or {}
        read, floor = dict(replies.get("read") or {}), str(replies.get("floor") or oldest)
        scan = now - self._thread_scan >= THREAD_SCAN_S
        try:
            msgs, posts = sl.poll(oldest)
            threads_new = sl.new_replies(posts + (sl.recent_posts() if scan else []), read, floor)
            siblings = projects_in_dm(sl.call("conversations.history", channel=sl.dm_channel(), limit=200)
                                      .get("messages", [])) or [self.p.name]
            uid = sl.resolve_user()
        except Exception as e:
            log(self.p, f"slack poll failed: {e}")
            return
        threads = set(db.kv("slack_threads", []))
        names = sorted(set(siblings) | {self.p.name})
        for m in msgs:
            text = route(m, self.p.name, threads, names)
            if text:
                with db.tx():   # stored exactly once: the message and the cursor past it commit together
                    db.post("in", text, chat="slack", channel="slack", kind="user", ref=m["ts"])
                    db.set_kv("slack_oldest", m["ts"])
                continue
            if not m.get("thread_ts") and names[0] == self.p.name \
                    and not re.match(r"^\s*[A-Za-z0-9._-]+\s*:", m.get("text") or ""):
                sl.post(self.p.name, f"Which project is this for? Start the message with one of: {', '.join(names)}, "
                                     f"e.g. `{self.p.name}: ...`", thread_ts=m["ts"])
            db.set_kv("slack_oldest", m["ts"])
        for parent, reps in threads_new:
            for r in reps:
                text = route(r, self.p.name, threads, names) if r.get("user") == uid else None
                read[parent] = r["ts"]
                with db.tx():
                    if text:
                        db.post("in", text, chat="slack", channel="slack", kind="user", ref=parent)
                    db.set_kv("slack_replies", {"floor": floor, "read": read})
        if scan:
            self._thread_scan = now
            # A thread older than both the window and the cursor is never read again.
            keep = min(now - THREAD_WINDOW_S, float(oldest))
            db.set_kv("slack_replies", {"floor": oldest, "read": {k: v for k, v in read.items() if float(k) >= keep}})

    # misc -------------------------------------------------------------------------------------------
    def _keep_awake(self) -> None:
        """On a Mac, hold off idle sleep while the daemon runs (on AC power only, by default)."""
        mode = self.cfg.get("power", {}).get("keep_awake", "on_ac")
        if sys.platform != "darwin" or mode in (False, "off", "never"):
            return
        flag = "-s" if mode == "on_ac" else "-i"
        try:
            subprocess.Popen(["caffeinate", flag, "-w", str(os.getpid())], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass


PAUSED_NOTE = "waits for a paused resource:"


def _exclusive(task: dict) -> list[str]:
    return [lb.split(":", 1)[1] for lb in json.loads(task["labels"] or "[]") if lb.startswith("exclusive:")]


def _shared(task: dict) -> list[str]:
    return [lb.split(":", 1)[1] for lb in json.loads(task["labels"] or "[]") if lb.startswith("resource:")]


def _cut_off_cost(run_dir: Path, exit_info: dict, usage=None, prov=None) -> float:
    """A run that ended without any usage to price still spent money: Codex reports usage only when
    a turn completes, and Cursor's result has none at all. Book the elapsed share of its dollar
    budget rather than $0, so the caps keep counting it. A run whose agent never started (it gave
    up waiting for a resource) spent nothing, and so did one that wrote no output and reported no
    tokens, when its CLI streams (a CLI that hung, or a host that slept, before its first event)."""
    if exit_info.get("launched") is False:
        return 0.0
    try:
        spec = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        return 0.0
    tokens = usage and _has_tokens(usage)
    try:
        silent = not (run_dir / "output.jsonl").stat().st_size
    except OSError:
        silent = True
    if silent and not tokens and (prov is None or prov.streams(spec.get("argv") or [])):
        return 0.0
    budget = float(spec.get("budget_usd") or spec.get("default_budget_usd") or 0)
    timeout = float(spec.get("timeout_s") or 0)
    elapsed = float(exit_info.get("ended") or time.time()) - float(exit_info.get("started") or 0)
    if budget <= 0 or timeout <= 0 or not exit_info.get("started"):
        return 0.0
    elapsed -= min(locks.waited(run_dir, float(exit_info.get("ended") or time.time())), timeout)
    return round(budget * min(max(elapsed, 0.0) / timeout, 1.0), 4)


def _watcher_timeout(payload: dict) -> int:
    """A command watcher's timeout: its timeout_s (default 120), capped at WATCHER_MAX_S so one slow
    watcher cannot hold a tick past the watchdog and restart the daemon over and over."""
    try:
        want = int(payload.get("timeout_s") or 120)
    except (TypeError, ValueError):
        want = 120
    return max(1, min(want, WATCHER_MAX_S))


def _before_stdin(argv: list[str], extra: list[str]) -> list[str]:
    """`argv` with `extra` added; a trailing "-" (prompt on stdin) stays the last argument."""
    return argv[:-1] + extra + ["-"] if argv[-1:] == ["-"] else argv + extra


def _with_system_prompt(provider: str, argv: list[str], path: Path) -> list[str]:
    if provider == "claude":
        return argv + ["--safe-mode", "--strict-mcp-config", "--tools", "", "--system-prompt", path.read_text()]
    return argv


def _observations(text: str) -> list[dict]:
    if not text:
        return []
    obs = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                o = json.loads(line)
                if isinstance(o, dict) and o.get("text"):
                    obs.append(o)
                    continue
            except ValueError:
                pass
        obs = []
        break
    return obs or [{"text": text[:6000]}]


def _result_ref(p: Project, path: Path) -> str:
    try:
        return str(path.relative_to(p.root))
    except ValueError:
        return str(path)


def _cut(text: str, n: int, where: str) -> str:
    """`text` within `n` characters; a cut one says so and where the whole text is."""
    if len(text) <= n:
        return text
    note = f" … [cut; the whole text is in {where}]"
    return text[:max(0, n - len(note))] + note


def _has_tokens(usage) -> bool:
    return bool(usage.input_tokens or usage.output_tokens or usage.cache_read_tokens or usage.cache_write_tokens)


def _resume_never_started(note: dict, usage) -> bool:
    """A run that was to continue a lost session ended before its agent did anything."""
    return bool(note.get("resumes")) and not usage.cost_usd and not usage.output_tokens


def _read_result(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _saved_gate(d: dict | None) -> bud.Gate | None:
    try:
        return bud.Gate(**d) if isinstance(d, dict) else None
    except TypeError:   # saved by a version with other fields
        return None


def _is_agent(pid: int, started: str) -> bool:
    """pid is still the agent its supervisor recorded: the leader of its own session, with the start
    token the supervisor wrote. A pid reused since then has another start, so it is never signalled."""
    try:
        return bool(started) and os.getsid(pid) == pid and runner.proc_start(pid) == started
    except OSError:
        return False


def _end_group(pgid: int, grace_s: float) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + grace_s
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _reboot_wakes(task: dict) -> int:
    """Boot-time wakes of a waiting task since it was last blocked, carried in its result."""
    try:
        return max(0, int(load_result(task["result"]).get("reboot_wakes") or 0))
    except (TypeError, ValueError):
        return 0


def _last_notes(run_dir: Path, n: int = 5) -> list[str]:
    """The run's last `ttp note` lines."""
    try:
        lines = (run_dir / "progress.md").read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [x[:300] for x in lines if x.strip()][-n:]


def _retry_s(result: dict) -> float:
    """A waiting hand-off's `retry_after_s`, kept between 5 minutes and 6 hours."""
    try:
        return min(max(float(result.get("retry_after_s") or 1800), 300.0), 6 * 3600.0)
    except (TypeError, ValueError):
        return 1800.0


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _flock(path: Path):
    """An fd holding an exclusive lock on path; False if another process holds it; None where
    the file system does not support flock."""
    try:
        import fcntl
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)   # not inherited by children (PEP 446)
    except (ImportError, OSError):
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return False
    except OSError:
        os.close(fd)
        return None


def _is_daemon(pid: int) -> bool:
    """pid is a live tt-project daemon, not a recycled pid now used by something else."""
    if not _alive(pid):
        return False
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "ttp" in cmd and "daemon" in cmd


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def heartbeat(p: Project) -> dict | None:
    """The daemon's last completed tick: {pid, host, started, age}, or None if it never ticked."""
    hb = p.state / "heartbeat"
    try:
        info = json.loads(hb.read_text())
        info["age"] = max(0.0, time.time() - hb.stat().st_mtime)
        return info
    except (OSError, ValueError, TypeError):
        return None


def start_marker(p: Project) -> dict | None:
    """The last daemon process to start: {pid, host, started, tick_errors}, or None."""
    try:
        info = json.loads((p.state / "daemon.start").read_text())
        return info if isinstance(info, dict) else None
    except (OSError, ValueError):
        return None


def sd_notify(msg: str, addr: str | None) -> bool:
    """Tell systemd about this service (the unit's WatchdogSec expects WATCHDOG=1 after each completed
    tick, or it restarts the daemon). No-op outside systemd."""
    if not addr:
        return False
    if addr[0] == "@":   # an abstract socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError:
        return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def main() -> int:
    return Daemon(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()).run()


if __name__ == "__main__":
    sys.exit(main())
