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
import uuid
from pathlib import Path

from . import alerts
from . import budget as bud
from . import globalcap as gcap
from . import coordinator as coord
from . import coordcheck
from . import effort
from . import ends
from . import integrity
from . import jevuse
from . import localspend
from . import locks
from . import machines
from . import prguard
from . import push
from . import pushq
from . import release
from . import runner
from . import schedule as sched
from . import unblock
from . import upstream
from . import screen as scr
from . import shared
from . import worktree
from .db import (OPEN_ASK_MAX_AGE_S, SEVERITY_RANK, TERMINAL_TASK_STATES, continues_id, deferral, dependency_ids,
                 dump_result, load_result, without_deferral)
from .project import (Project, deep_merge, layered, disk_resume_gb, durable_write, git_fsync_env, hostname,
                      nice_level, push_allowed, zombie)
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
# What a review the daemon queues repeats of the code task's spec and hand-off.
AUTO_REVIEW_SPEC_CHARS, AUTO_REVIEW_SUMMARY_CHARS = 2000, 1000
# A failed review with fix specs gets its fix and re-review from the daemon this many rounds per stack;
# the re-review repeats the failed review's spec (its push and after-push steps) up to this length.
AUTO_FIX_ROUNDS, REVIEW_FIX_SPEC_CHARS = 2, 8000
PROBE_EVERY_S = 180     # how often a waiting task's `retry_when` (or a deferred one's `start_when`) probe runs
NOT_YET_RCS = (1, 75, 255)   # probe exits meaning "not yet": 1, EX_TEMPFAIL (a busy `ttp lock`), ssh unreachable
PROBE_TIMEOUT_S = 60
AUTH_PROBE_S = 900      # while a provider without a login check is logged out, one run on it checks this often
AUTH_CHECK_S = (60, 120, 300, 600, 1200, 1800)   # backoff of the model-free login checks of an open breaker
ORPHAN_GRACE_S = 10     # TERM to KILL for an agent whose supervisor died
HANDOFF_STATES = ("done", "blocked", "failed", "needs_review", "waiting")
SLEEP_CUT = ("timeout", "stalled", "lost", "failed")   # ends a host sleep can cause
DISK_LIGHT_KINDS = ("question", "plan")   # the only task kinds that still start under the disk guard
DISK_RESUME = 1.2        # the guard ends once free space is this many times its threshold
DISK_FLOOR_GB = 2        # below this even questions and plans wait
DISK_DU_TIMEOUT_S = 30   # the guard alert's du breakdown stops after this, keeping what it measured
DISK_DU_TOP = 6          # the biggest top-level directories it names
KV_WORKTREES_LOGGED = "worktrees_logged"   # task -> the keep reason the daemon log last gave
KV_WORKTREES_DIRTY = "worktrees_dirty"     # task -> modified tracked files a kept finished worktree holds
KEEP_RECHECK_S = 6 * 3600   # a finished task's kept worktree is looked at again this often
CONFIG_UNREADABLE_KEY = "config_unreadable"   # kv: project.json and its last good copy both unreadable
ALERT_KEEP_S = 30 * 86400   # alerts_sent keeps an entry this long: the longest every_s any alert uses
SLEPT_MIN_S = 60        # a run whose wall clock ran this much ahead of its monotonic clock overlapped a host sleep
SLEEPS_KEPT_S = 7 * 86400
SLEPT_LONG_S = 600      # a sleep this long is recorded as a `host_slept` event and tells the user once a day
SLEEP_EVENT_MERGE_S = 1800   # a sleep this soon after the last one (the dark wakes of a closed lid) extends its event
# A run that ended because the provider's API could not be reached (DNS gone after a host sleep, a
# network drop) is lost to the network, not an attempt; new runs on that provider wait (net_held).
NET_LOST_RE = re.compile(r"Can't reach the API server|ENOTFOUND|EAI_AGAIN")
# A resumed session's reported cost is netted of what its earlier runs booked only when the
# difference is at least this share of the run's own token-priced cost, less the slack (in dollars).
SESSION_NET_AGREE = 0.5
SESSION_NET_SLACK_USD = 0.05
REACH_EVERY_S = 30      # while a provider is held offline, how often its API host is resolved again
REACH_TIMEOUT_S = 5
NET_HOLD_MAX_S = 900    # a held provider still lets one run try this often: a lookup that keeps failing never holds forever
PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")   # set: the host resolves nothing itself
LOCAL_ONLY_EVERY_S = 3600   # how often done code tasks' branches are checked against the remotes
LOCAL_ONLY_DAYS = 14        # done code tasks finished this recently are checked (flagged ones until cleared)
KV_LOCAL_ONLY = "local_only"   # kv: {task id: {branch, head, ahead, since}} for branches only on this machine
KV_INTEGRITY = "integrity"   # kv: the last boot integrity check (see Daemon.check_integrity)
INTEGRITY_RECHECK_S = 3600
KV_LOCAL_ONLY_FROM = "local_only_from"   # kv: when the check first ran; tasks done before it are not checked
KV_BACKUP = "backup_pending"   # kv: {task id: {branch, tries, next}}: branches for delivery.backup_remote
BACKUP_TRIES = 3            # a backup push that fails this often (network, auth) is given up with an observation
BACKUP_RETRY_S = 900        # times the try count: the wait before a failed backup push is tried again
KV_DIRTY_MAIN = "dirty_main"   # kv: key of the main checkout's dirty tracked paths last reported
PUSH_REFS_EVERY_S = 600   # how often the push queue's pins (refs/ttp/push/<id>) of settled rows are pruned


def log(p: Project, msg: str) -> None:
    p.logs.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n"
    with open(p.logs / "daemon.log", "a") as f:
        f.write(line)


def _du(args: list[str], timeout: float) -> tuple[dict[str, int], bool]:
    """{path: bytes} from `du -k` (unreadable directories are skipped), and whether it finished
    within `timeout`; what it printed before it was stopped is kept."""
    try:
        r = subprocess.run(["du", "-k", *args], capture_output=True, timeout=max(timeout, 1))
        out, done = r.stdout, True
    except subprocess.TimeoutExpired as e:
        out, done = e.stdout or b"", False
    except OSError:
        return {}, False
    sizes = {}
    for ln in (out.decode(errors="replace") if isinstance(out, bytes) else out).splitlines():
        kb, _, name = ln.partition("\t")
        if kb.strip().isdigit() and name:
            sizes[name] = int(kb) * 1024
    return sizes, done


def _mount_of(path: Path) -> Path:
    if os.environ.get("TTP_TEST_DISK_MOUNT"):   # tests measure a small folder, not the real disk
        return Path(os.environ["TTP_TEST_DISK_MOUNT"])
    p = path.resolve()
    while not os.path.ismount(p) and p.parent != p:
        p = p.parent
    return p


def disk_breakdown(p: Project, path: Path, timeout: float = DISK_DU_TIMEOUT_S) -> dict:
    """What fills the filesystem holding `path`, so a full shared disk is not taken for project growth:
    this project's own data on it (its root, and its worktrees wherever they are) and the biggest
    top-level directories (du -x -d 1). The project gets at most half of `timeout`; each du keeps
    what it measured when stopped."""
    deadline = time.monotonic() + timeout
    mount = _mount_of(path)
    try:
        dev = os.stat(mount).st_dev
    except OSError:
        return {"mount": str(mount), "top": [], "complete": False, "own_bytes": None, "own_complete": False}
    roots = []
    for d in (p.root.resolve(), p.worktrees.resolve()):
        try:
            if os.stat(d).st_dev == dev and not any(d == r or r in d.parents for r in roots):
                roots.append(d)
        except OSError:
            continue
    own, own_done = 0, True
    for d in roots:
        sizes, done = _du(["-x", "-s", str(d)], min(deadline - time.monotonic(), timeout / 2))
        own += sum(sizes.values())
        own_done = own_done and done and bool(sizes)
    sizes, done = _du(["-x", "-d", "1", str(mount)], deadline - time.monotonic())
    sizes = {k: v for k, v in sizes.items() if Path(k) != mount}   # the total line
    top = sorted(sizes.items(), key=lambda kv: -kv[1])[:DISK_DU_TOP]
    return {"mount": str(mount), "top": top, "complete": done, "own_bytes": own if roots else None,
            "own_complete": own_done}


def disk_usage_line(b: dict, used: float) -> str:
    """One sentence for the guard alert: this project's share of the used space, then the biggest
    top-level directories."""
    gb = lambda n: f"{n / 1e9:.1f} GB"   # noqa: E731
    parts = []
    if b.get("own_bytes") is not None:
        partial = "" if b.get("own_complete") else " or more (du stopped early)"
        parts.append(f"This project's own data is {gb(b['own_bytes'])}{partial} of the {gb(used)} used on {b['mount']}")
    else:
        parts.append(f"{gb(used)} used on {b['mount']}")
    if b.get("top"):
        partial = "" if b.get("complete") else f" (du stopped after {DISK_DU_TIMEOUT_S} s; partial)"
        parts.append(f"biggest top-level directories{partial}: "
                     + ", ".join(f"{name} {gb(n)}" for name, n in b["top"]))
    else:
        parts.append(f"du measured no top-level directory within {DISK_DU_TIMEOUT_S} s")
    return "; ".join(parts) + "."


JEV_FUNDS_TEXT = ("The Jev account is out of credits. Screening and the other Jev checks fall back to rules "
                  "(more model calls, same coverage). Top up the Jev account to restore the savings.")

class Daemon:
    def __init__(self, base: str | Path):
        self.p = Project(base)
        self.stopping = False
        self.boot = runner.boot_id()
        self.boot_at = runner.boot_time()
        self.gates: dict[str, bud.Gate] = {}
        self.cfg_status = "ok"
        self._load_config()
        self.jev = Jev(self.cfg, db=self.p.db)
        self._slack = None
        self._last_cfg = 0.0
        self._trouble_checked = 0.0
        self._upstream_checked = 0.0
        self._forwarded = 0.0
        self._forwarder: threading.Thread | None = None
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
        self._local_only_due = 0.0   # when done code tasks' branches are next checked for remote copies
        self._push_refs_at = 0.0     # when the push queue's pins were last pruned
        self._local_only_ok: dict[str, str] = {}   # branch -> head found on a remote: not looked at again
        self._local_only_job: dict | None = None   # the check running in its thread, applied by a later tick
        self._backup_job: dict | None = None   # the backup pushes running in their thread (backup_branches)
        if not isinstance(self.p.db.kv(KV_LOCAL_ONLY_FROM), (int, float)):
            self.p.db.set_kv(KV_LOCAL_ONLY_FROM, time.time())   # work done before the check existed is not flagged
        self._kept: dict[int, tuple[float, float, str]] = {}   # task id -> (task updated, checked, why kept)
        self._disk_low = bool(self.p.db.kv("disk_low"))   # an episode outlives a restart: no second alert
        self._disk_free: float | None = None
        self._tick_errors = 0
        self._probes: dict[int, tuple[subprocess.Popen, float, str]] = {}   # running: proc, started, probe
        self._probed: dict[int, float] = {}
        self._ends = ends.Ends(self.p, log=lambda m: log(self.p, m))   # temporary instructions' end conditions
        self._probe_rc: dict[int, tuple[int | str, float, str]] = {}   # last verdict: exit code or why, when, probe
        self._reboot_told = False
        self._boot_woken = False
        self._held: list[str] | None = None   # lock holders the heartbeat file last recorded
        self._notify: str | None = None   # systemd's socket for the watchdog ping, when it runs us
        self._tick_wall, self._tick_mono = time.time(), time.monotonic()
        self._settle_until = 0.0   # monotonic time before which nothing new starts (the host just woke)
        # provider -> {host, since, checked, checking, up, probe}: its API host must resolve before
        # new runs start on it (net_held). In memory only: a restart tries the network afresh.
        self._net_holds: dict[str, dict] = {}
        self._sched_sig: tuple[int, int] | None = None   # harness/schedules.json (mtime, size) last checked
        self._sched_read = 0.0   # monotonic time it was last read
        self._sched_problem: str | None = None   # what was wrong with it, last logged
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
        self._check_config()
        self.sync_schedules(start=True)
        from .web import serve
        threading.Thread(target=serve, args=(self,), daemon=True).start()
        self._keep_awake()
        self.check_integrity(start=True)
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
        self._ends.stop()   # end-condition probes are rerun after the next start
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

    def _load_config(self) -> None:
        """Reload project.json. Missing or broken, its last good copy stays in force; with none, an
        established project starts no coordinator turn or worker run (both would route on the
        defaults) and says so in one alert, which clears once the file reads again."""
        raw, status = self.p.read_config()
        self.cfg = layered(raw)
        if status != self.cfg_status:
            log(self.p, {"ok": "project.json reads again",
                         "fallback": "project.json is missing or not valid JSON: its last good copy stays in force",
                         "unavailable": "project.json is missing or not valid JSON and there is no last good copy: "
                                        "no new coordinator turns or worker runs"}[status])
        self.cfg_status = status
        db = self.p.db
        bad = status == "unavailable"
        if bad != bool(db.kv(CONFIG_UNREADABLE_KEY)):
            db.set_kv(CONFIG_UNREADABLE_KEY, bad)
        if bad:
            self.alert("config", f"{self.p.config_path} is missing or not valid JSON, and there is no last good "
                                 f"copy to fall back on. No coordinator turns or worker runs start until it reads "
                                 f"again (running work goes on); its earlier versions are in the harness git history.",
                       every_s=ALERT_KEEP_S)

    def _check_config(self) -> None:
        """Say once (per distinct finding) what in project.json no code reads, or which push check
        is no command, so a typo or a sentence there does not fail a push much later."""
        try:
            from .project import config_problems
            probs = config_problems(self.p.raw_config())
            if probs:
                key = "config_problems:" + hashlib.sha1("\n".join(probs).encode()).hexdigest()[:10]
                self.alert(key, "project.json: " + "; ".join(probs), severity="low", every_s=ALERT_KEEP_S)
        except Exception:
            log(self.p, "config check: " + traceback.format_exc().replace("\n", " | ")[:1000])

    def sync_schedules(self, start: bool = False) -> None:
        """Apply harness/schedules.json when it changed (at start, always). A hand edit is committed
        so it too leaves a trail; a file that does not check out is reported and left unapplied."""
        sig = None
        try:
            st = sched.file_path(self.p).stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        # The content is compared too, once a minute: an edit can keep both mtime and size.
        if not start and sig == self._sched_sig and time.monotonic() - self._sched_read < 60:
            return
        applied, problem = sched.sync_file(self.p, force=start)
        self._sched_sig, self._sched_read = sig, time.monotonic()
        if applied:
            log(self.p, f"applied {sched.FILE}")
            self.p.commit_harness([sched.file_path(self.p)], f"schedules: {sched.FILE} applied")
        if problem and problem != self._sched_problem:
            log(self.p, problem)
        self._sched_problem = problem
        if problem:
            key = "schedules_file:" + hashlib.sha1(problem.encode()).hexdigest()[:10]
            self.alert(key, f"{problem}. The schedules stay as they were until it is fixed.", severity="low",
                       every_s=ALERT_KEEP_S)

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
        held = shared.held(self.p, self.cfg)
        if not self._healthy or held != self._held:
            durable_write(hb, json.dumps({"pid": os.getpid(), "host": hostname(), "started": self._started,
                                          "boot": self.boot, "held": held}))
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
            self._load_config()
            self._last_cfg = now
            self.jev = Jev(self.cfg, db=self.p.db)
        for step in (self.reap_runs, self.wake_after_reboot, self.meter_running, self.reconcile_tasks, self.tend_pushes,
                     self.prune_worktrees, self.backup_branches, self.check_local_only, self.check_disk,
                     self.sweep_alerts, self.check_release, self.sync_shared_pauses, self.check_integrity,
                     self.sync_schedules, self.lint_charter):
            step()
            self._progress()
        if self.p.db.kv("paused", False):
            return
        self._refresh_meters()
        self.update_gates()
        coord.expire_asks(self.p, hold=any(g.level == "red" for g in self.gates.values()))
        scr.expire_mutes(self.p.db)
        scr.close_watcher_issues(self.p.db, quiet_s=scr.WATCHER_QUIET_CLOSE_S)
        self.review_jev()
        settling = self.settling()
        core = self.cfg.get("core_provider") or "claude"
        core_held = self.net_held(core) and not self._net_may_probe(core)
        for step in (self.run_schedules, self.poll_slack, self.check_resource_trouble, self.read_upstream,
                     self.forward_upstream, self.retry_rejected, self.retire_ended, self.maybe_coordinate, self.probe_waiting, self.start_pushes, self.dispatch, self.deliver_outbound):
            if self.cfg_status == "unavailable" and step in (self.maybe_coordinate, self.dispatch):
                continue   # no routing to start model work with (see _load_config)
            # While the host settles after a sleep only new work waits: a person who wrote is answered now.
            # So while the core provider's API host does not resolve; dispatch holds each provider itself.
            if settling and step == self.dispatch or (settling or core_held) and step == self.maybe_coordinate \
                    and not self.p.db.one("SELECT id FROM messages WHERE direction='in' AND handled=0"):
                continue
            step()
            self._progress()

    def review_jev(self, every_s: float = 600) -> None:
        """Switch off the Jev uses that do not save money (jevuse.review) and report each one once."""
        now = time.time()
        if now - getattr(self, "_jev_reviewed", 0.0) < every_s:
            return
        self._jev_reviewed = now
        for use, s in jevuse.review(self.p.db, self.cfg, now):
            self.alert(f"jev-off:{use}", jevuse.off_text(use, s, self.cfg), severity="low", every_s=0)

    def retire_ended(self) -> None:
        """Retire memory entries and charter sections whose end condition passed (see ends)."""
        try:
            self._ends.tick()
        except Exception:
            log(self.p, "retiring ended instructions failed\n" + traceback.format_exc())

    def sync_shared_pauses(self) -> None:
        coord.sync_shared_pauses(self.p)

    def tend_pushes(self) -> None:
        """Start the detached pushes queued from inside a sandbox; record the dead ones as failed. Then
        the push queue's bookkeeping, also while paused: approvals whose review moved on, a queue
        turned off, and batches that ended (pushq.tend). Pins of settled rows go now and then."""
        push.tend(self.p)
        try:
            changed = pushq.tend(self.p, self.cfg, self.alert, may_requeue=self.cfg_status != "unavailable")
        except Exception:
            log(self.p, "push queue: finalize failed\n" + traceback.format_exc())
            return
        now = time.time()
        if changed or now - self._push_refs_at > PUSH_REFS_EVERY_S:
            self._push_refs_at = now
            try:
                pushq.prune_refs(self.p)
            except Exception:
                log(self.p, "push queue: pruning pinned refs failed\n" + traceback.format_exc())

    def start_pushes(self) -> None:
        """Start a push batch when one is due (pushq.schedule): local checks only, no model."""
        try:
            pushq.schedule(self.p, self.cfg, log=lambda m: log(self.p, m))
        except Exception:
            log(self.p, "push queue: starting a batch failed\n" + traceback.format_exc())

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
        # The network may not be back with the host: new runs wait until the API hosts resolve.
        for prov in {self.cfg.get("core_provider") or "claude", *[t["provider"] for t in db.q(
                "SELECT DISTINCT provider FROM tasks WHERE provider IS NOT NULL AND status='queued'")]}:
            self.hold_offline(prov, "the host slept")
        if jump >= SLEPT_LONG_S:
            self._record_sleep(since, wall, jump)

    def _record_sleep(self, since: float, woke: float, slept: float) -> None:
        """A long sleep. Running runs need no clock change: their supervisors count timeout and stall
        in awake time. Record it as one `host_slept` event per sleepy stretch (a closed lid wakes in
        the dark many times), and tell the user once a day what keeps the project awake."""
        db = self.p.db
        runs = [r["id"] for r in db.q("SELECT id FROM runs WHERE status='running'")]
        last = db.one("SELECT id, data FROM events WHERE kind='host_slept' ORDER BY id DESC LIMIT 1")
        data = json.loads(last["data"] or "{}") if last else {}
        if last and float(data.get("woke") or 0) >= since - SLEEP_EVENT_MERGE_S:
            data.update(woke=woke, slept_s=round(float(data.get("slept_s") or 0) + slept),
                        sleeps=int(data.get("sleeps") or 1) + 1,
                        runs=sorted(set(data.get("runs") or []) | set(runs)))
            db.x("UPDATE events SET text=?, data=? WHERE id=?",
                 (_sleep_text(data), json.dumps(data), last["id"]))
        else:
            data = {"since": since, "woke": woke, "slept_s": round(slept), "sleeps": 1, "runs": runs}
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
                 (woke, "host", "host_slept", "normal", _sleep_text(data), json.dumps(data), "record"))
        self.alert("host_slept",
                   f"{hostname()} slept for {slept / 60:.0f} min and the project stood still meanwhile"
                   f"{f' (runs {_ids(runs)} paused)' if runs else ''}. Keep-awake only stops idle sleep, "
                   f"not a closed lid. To keep working the project needs one of: the lid open, power "
                   f"plus an external display (clamshell mode), or an always-on host.",
                   "low", every_s=86400)

    def settling(self) -> bool:
        """The host woke from a sleep less than wake_settle_s of awake time ago: hold new runs."""
        return time.monotonic() < self._settle_until

    def hold_offline(self, prov: str, why: str) -> None:
        """New runs on `prov` wait until its API host resolves again (net_held). A host that reaches
        the API through a proxy resolves nothing itself, so it is never held."""
        if _proxied():
            return
        try:
            host = get_provider(prov).reach_host()
        except Exception:
            host = ""
        if not host:
            return
        self._net_holds[prov] = {"host": host, "since": time.monotonic(), "checked": None, "checking": False,
                                 "up": False, "probe": None}
        log(self.p, f"{prov}: {why}; no new runs start on it until {host} resolves")

    def net_held(self, prov: str) -> bool:
        """Whether new runs on `prov` wait for its API host to resolve. The lookup runs in a thread of
        its own, at most every REACH_EVERY_S, so a tick never waits on DNS. A hold that lasts
        NET_HOLD_MAX_S lets one run try anyway (_net_may_probe); how it ends re-arms or ends it."""
        h = self._net_holds.get(prov)
        if not h:
            return False
        now = time.monotonic()
        if not h["up"] and not h["checking"] and (h["checked"] is None or now - h["checked"] >= REACH_EVERY_S):
            h["checking"], h["checked"] = True, now

            def look(h=h) -> None:
                try:
                    h["up"] = _resolves(h["host"])
                finally:
                    h["checking"] = False
            _background(look)
        if h["up"]:
            self._end_net_hold(prov, f"{h['host']} resolves")
            return False
        return True

    def _net_may_probe(self, prov: str) -> bool:
        """Whether one run on held `prov` may start to try the network: the hold (or its last such
        try) is NET_HOLD_MAX_S old. A run that starts on a held provider is that try (start_run)."""
        h = self._net_holds.get(prov)
        if not h:
            return False
        # No try yet: count from the hold. The monotonic clock starts near 0 at boot, so 0.0 is no "never".
        last = h["since"] if h["probe"] is None else max(h["since"], h["probe"])
        return time.monotonic() - last >= NET_HOLD_MAX_S

    def _end_net_hold(self, prov: str, why: str) -> None:
        if self._net_holds.pop(prov, None):
            log(self.p, f"{prov}: {why}; new runs start on it again")

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

    def _coordinator_effort(self, provider: str, tier: str) -> str:
        """The coordinator's effort before any unblock raise: coordinator.effort, else its tier's."""
        tiers = self.cfg["providers"].get(provider, {}).get("tiers", {})
        return str((self.cfg.get("coordinator") or {}).get("effort") or "") or tiers.get(tier, {}).get("effort", "")

    def _coord_check(self, provider: str, tier: str, event_ids: list[int], wake_due: str | None) -> dict | None:
        """Jev's 'routine or needs thought?' verdict on a turn the rules leave below unblock_effort
        (coordcheck), or None: not needed, off, or failed (the rules' choice stands)."""
        base = self._coordinator_effort(provider, tier)
        high = coord.raise_effort(base, str(self.cfg["coordinator"].get("unblock_effort", "high") or ""))
        if high == base:
            return None   # already at unblock_effort: the check could not change it
        try:
            return coordcheck.check(self.p.db, self.cfg, self.jev,
                                    coordcheck.summary(self.p.db, event_ids, wake_due), base, high)
        except JevOutOfFunds:
            self.alert("jev-funds", JEV_FUNDS_TEXT, "high")
        except Exception:   # the check must never hold a turn back
            log(self.p, "coordinator jev check failed: " + traceback.format_exc().replace("\n", " | ")[:1000])
        return None

    def start_run(self, role: str, prompt: str, provider: str, tier: str, cwd: str, *, task: dict | None = None,
                  budget_usd: float | None = None, timeout_s: float | None = None, read_only: bool = False,
                  schema: dict | None = None, system: str | None = None, append_system: str | None = None,
                  note: dict | None = None, resume: str | None = None, unblock: str = "",
                  context: str | None = None, cache_ttl: str = "") -> int:
        """`context` (with `system`): stable text that follows the system prompt, sent as a block of
        its own with a cache breakpoint after it where the provider can mark one (else it ends the
        system prompt). `cache_ttl` (`5m`, `1h`) sets the provider's prompt cache lifetime."""
        if self.cfg_status == "unavailable":
            raise RuntimeError("project.json is unreadable with no last good copy: no model work starts")
        if provider in self._net_holds:
            self._net_holds[provider]["probe"] = time.monotonic()   # the one run that tries the network
        tiers = self.cfg["providers"].get(provider, {}).get("tiers", {})
        model = tiers.get(tier, {}).get("model", "")
        effort = tiers.get(tier, {}).get("effort", "")
        if role == "coordinator":
            # coordinator.model / coordinator.effort pin the coordinator, so moving its tier's model
            # (say light to a cheaper one for workers) does not move the coordinator with it.
            c = self.cfg.get("coordinator") or {}
            model = str(c.get("model") or "") or model
            effort = str(c.get("effort") or "") or effort
            if unblock and not str(c.get("effort") or ""):
                # A tricky or blocking turn thinks harder (raise only); a pinned effort wins.
                effort = coord.raise_effort(effort, str(c.get("unblock_effort", "high") or ""))
        prices = (self.cfg.get("pricing") or {}).get(provider) or {}
        prov = get_provider(provider).use(model, prices)
        restrictions = self.cfg.get("restrictions", {})
        if read_only and prov.isolate_read_only:
            cwd = scratch_dir(str(self.p.base))
        argv, env = prov.build(role=role, model=model, effort=effort, cwd=cwd, budget_usd=budget_usd,
                               read_only=read_only, schema=schema, restrictions=restrictions)
        env = {**env, **prov.cache_env(cache_ttl)}
        if role != "coordinator":
            window = self.cfg["budget"].get("compact_window_tokens") or 0   # 0: off for every tier
            window = window.get(tier) if isinstance(window, dict) else window
            env = {**env, **prov.compact_env(int(window or 0))}
            argv = _before_stdin(argv, prov.compact_args(int(window or 0)))
            argv = prov.cap_output(argv, int(self.cfg["budget"].get("bash_output_max_chars") or 0))
        mcp_servers: dict = {}
        resume_extra: list[str] = []
        private: list[str] = []   # files that may hold credentials, removed when the run ends
        sandboxed = False   # the agent fences the worker's writes (see Provider.writable_args)
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
            roots = [str(self.p.state)] + worktree.git_dirs(Path(cwd))
            # `ttp lock` on a resource shared across projects takes its slot under the user's shared
            # lock root, outside the project: only that directory, not the rest of the user's
            # tt-project home. It must exist for the sandbox to grant it.
            try:
                shared.root().mkdir(parents=True, exist_ok=True)
                roots += list(dict.fromkeys([str(shared.root()), str(shared.root().resolve())]))
            except OSError:
                pass   # `ttp lock` then says plainly that it cannot write the shared lock
            fence = prov.writable_args(roots)
            sandboxed = bool(fence)
            extra = fence + prov.plugin_args([d for d in dirs if d not in missing])
            if self.cfg["providers"].get(provider, {}).get("worker_isolation"):
                extra += prov.isolation_args()
                mcp_servers = self._approved_mcp(prov, provider, cwd)
            # A continued session (see _resumable) gets the current system prompt again, below. Its
            # arguments go last: Codex takes them as a subcommand that must follow every option.
            resume_extra = prov.resume_args(resume) if resume else []
            argv = _before_stdin(argv, extra)
        # The agent's session gets its id here, so the run's session is known (and left out of this
        # machine's other Claude Code spend, localspend.py) before the agent writes a line of it.
        session_id = resume if resume_extra else ""
        if not resume_extra:
            fresh = str(uuid.uuid4())
            got = prov.session_args(fresh)
            if got:
                argv, session_id = _before_stdin(argv, got), fresh
        db = self.p.db
        run_id = db.x("INSERT INTO runs(task,role,provider,model,effort,account,started,boot_id,status,note,"
                      "session_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (task["id"] if task else None, role, provider, model, effort, prov.account(), time.time(),
                       self.boot, "running", json.dumps(note or {}), session_id or None))
        run_dir = self.p.runs / str(run_id)
        # Raising from here on means nothing was launched: the run row must not stay "running".
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            # A fenced worker gets a temp dir of its own under the run dir (inside state/, a writable
            # root; never in the worktree): on macOS git's xcrun shim otherwise fails to write its
            # cache, and tools that honour TMPDIR stay off the shared /tmp. The runner removes it when
            # the run ends (remove_private if the runner died).
            tmp_env = {}
            if sandboxed:
                (run_dir / "tmp").mkdir(exist_ok=True)
                tmp_env = {var: str(run_dir / "tmp") for var in ("TMPDIR", "TMP", "TEMP")}
            if mcp_servers:
                # Outside the repo and the run directory, owner-only: server entries can carry tokens.
                fd, mcp_path = tempfile.mkstemp(prefix=f"ttp-mcp-{run_id}-", suffix=".json")
                private.append(mcp_path)
                with os.fdopen(fd, "w") as f:
                    json.dump({"mcpServers": mcp_servers}, f)
                argv = prov.with_mcp_config(argv, Path(mcp_path))
            stdin = None
            if system is not None and context is not None:
                cached = prov.cached_input(context, prompt, cache_ttl, log=lambda m: log(self.p, m))
                if cached:
                    argv, stdin = _before_stdin(argv, cached[0]), cached[1]
                else:
                    system = coord.join_prompt(system, context)
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
            if stdin is not None:   # what the agent reads; prompt.md stays the readable digest
                (run_dir / "input.jsonl").write_text(stdin)
            runtime_dir = str(Path(__file__).resolve().parent.parent)
            path = f"{service_path()}:{os.environ.get('PATH', '')}"
            # The project's venv, so a fresh worktree need not build one; `ttp` stays first.
            venv = worktree.project_venv(self.p, cwd) if role in ("worker", "reviewer") and not read_only else None
            venv_vars = worktree.venv_env(venv, path) if venv else {}
            env = {**env, **venv_vars, **tmp_env, "TTP_RUN_DIR": str(run_dir), "TTP_PROJECT": str(self.p.base),
                   "TTP_RUN_ID": str(run_id), "TTP_TASK": str(task["id"]) if task else "", "PYTHONPATH": runtime_dir,
                   "TTP_PYTHON": sys.executable,   # `ttp` runs under this, not the venv's python3
                   # A worker's sandbox may run commands in a PID namespace of their own, which ends
                   # with the command: `ttp push --detach` compares and has the daemon start the push.
                   **({"TTP_PIDNS": ns} if (ns := push.pid_ns()) else {}),
                   "PATH": f"{self.p.harness / 'bin'}:{venv_vars.get('PATH', path)}"}
            env.update(git_fsync_env({**os.environ, **env}))   # a power cut must not corrupt workers' commits
            tout = timeout_s or self.cfg["budget"]["run_timeout_s"].get(tier, 3600)
            stall = self.cfg["budget"].get("stall_s", {}).get(tier) if role != "coordinator" else None
            spec = {"argv": argv, "env": env, "cwd": cwd, "timeout_s": tout, "provider": provider, "stall_s": stall,
                    "model": model, "prices": prices, **({"stdin": "input.jsonl"} if stdin is not None else {}),
                    "budget_usd": budget_usd if provider not in ("claude",) else None,
                    # What a run without its own budget is priced at when it reports no usage.
                    "default_budget_usd": self.cfg["budget"].get("task_default_usd", {}).get(tier, 8.0),
                    "exclusive": [{"resource": res, "lock": locks.canonical(self.cfg, res),
                                   "paths": [str(x) for x in self._slot_paths(res)],
                                   "reserve": str(self._reserve_path(res)),
                                   "holder": shared.holder(self.p, res, f"task #{task['id']}", self.cfg)}
                                  for res in _exclusive(task)] if task else [],
                    "exclusive_wait_s": self.cfg["budget"].get("exclusive_wait_s", 600),
                    "private_files": private, "tmp_dir": tmp_env.get("TMPDIR"),
                    # Workers and reviewers run niced, and all they start with them; the coordinator not.
                    "nice": nice_level(self.cfg.get("runner"))[0] if role != "coordinator" else 0}
            durable_write(run_dir / "run.json", json.dumps(spec, indent=1))   # read again after a reboot
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
        self._raise_review_stalls(now)

    def _raise_review_stalls(self, now: float) -> None:
        """Only 'done' satisfies a dependency, so a task left in 'review' holds its dependents back
        without ever looking dead. Past coordinator.review_stall_s it is raised once per review stint."""
        db = self.p.db
        try:
            stall_s = float(self.cfg["coordinator"].get("review_stall_s", 14400) or 0)
        except (TypeError, ValueError):
            stall_s = 14400.0
        for rev, since, deps in db.stalled_reviews(stall_s, now):
            fp = f"review_stall:{rev['id']}:{since:.0f}"
            if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                continue
            names = ", ".join(f"#{t['id']} {t['title'][:80]}" for t in deps[:5]) + (
                f" and {len(deps) - 5} more" if len(deps) > 5 else "")
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", "review_stall", fp, "normal",
                  f"#{rev['id']} {rev['title']} has been in review {(now - since) / 3600:.1f}h; queued {names} "
                  f"depend on it and cannot start until it is done. Options: get it reviewed, mark it done with "
                  f"task_update status done once its work is verified, requeue it, re-point the dependents with "
                  f"task_update depends_on, or cancel them.", "queued", rev["id"]))
            log(self.p, f"task {rev['id']} in review {(now - since) / 3600:.1f}h holds {len(deps)} task(s); "
                        f"raised to the coordinator")

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
        before it ends. `finish_run` replaces the figures with the final ones."""
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
                usage = get_provider(r["provider"]).parse(out)
                self._priced(r, usage)
                cost = self._net_of_session(r, usage)
            except Exception:
                continue
            # The tokens also show a run on a logged-out provider got past the login (alerts.holds).
            self.p.db.x("UPDATE runs SET cost_usd=?, cost_estimated=1, input_tokens=?, output_tokens=?, "
                        "cache_read_tokens=? WHERE id=? AND status='running'",
                        (cost, usage.input_tokens or 0, usage.output_tokens or 0, usage.cache_read_tokens or 0,
                         r["id"]))
            self._suggest_split(r, usage.cache_read_tokens or 0)

    def _suggest_split(self, r: dict, reread: int) -> None:
        """Once a worker's run has re-read more context than `budget.split_reread_tokens` allows for
        its tier, tell it (through steer.md, once) to finish the step it is on and hand the rest on
        as a follow-up: a fresh run starts from a small context instead of re-reading a huge one on
        every call."""
        if r["role"] == "coordinator" or not r["task"] or not r["dir"]:
            return
        limit = self.cfg["budget"].get("split_reread_tokens") or 0   # 0: off for every tier
        task = self.p.db.task(r["task"]) or {}
        if isinstance(limit, dict):
            limit = limit.get(task.get("tier") or "standard") or 0
        if not limit or reread < int(limit):
            return
        head = (f"This run has re-read about {reread / 1e6:.1f} M tokens of context (the split line for its tier "
                f"is {int(limit) / 1e6:.1f} M); every further call re-reads it all again. ")
        if r["role"] == "reviewer" or task.get("kind") == "review":
            # A review's `done` approves and its followups are blocking findings: it cannot hand half on.
            text = head + ("A review does not split: finish it in as few further calls as you can, reading "
                           "only what is still unchecked. If you already have blocking findings, hand off "
                           "`failed` with them now. Never hand off `done` without having checked the whole change.")
        else:
            text = head + ("Finish the step you are on, commit, and hand off: `done` with a `followups` entry "
                           "(title starting `continue:`) whose spec is self-contained (what is done, the branch "
                           "and head, what is left), or `waiting` if that fits. Do not start new large steps in "
                           "this run.")
        coord._append_update(Path(r["dir"]) / "steer.md", text, f"split-{r['id']}")

    def _priced(self, r: dict, usage) -> float:
        if usage.estimated and not usage.cost_usd:
            usage.cost_usd = bud.estimate_cost(self.p.db, self.cfg, r["provider"], r["model"] or "", {
                "input": usage.input_tokens, "output": usage.output_tokens,
                "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens})
        return usage.cost_usd

    def _net_of_session(self, r: dict, usage) -> float:
        """A resumed Claude Code session reports the whole session's total_cost_usd, not this run's:
        book only what this run added beyond the earlier runs of the session. Netted only when the
        report covers what those runs booked and the difference agrees with this run's own tokens
        priced at the project's rate; even then never below that price. Otherwise the full report
        is booked: over-booking is the safe failure, under-booking lets spend run away."""
        session = (json.loads(r["note"] or "{}").get("resumes") or {}).get("session")
        reported = float(usage.cost_usd or 0)
        if not session or usage.estimated or reported <= 0:
            return usage.cost_usd
        # By runs.session_id; rows from before that column fall back to the session id in their note.
        booked = sum(float(o["cost_usd"] or 0) for o in self.p.db.q(
            "SELECT cost_usd, session_id, note FROM runs WHERE id<? AND task IS ? AND "
            "(session_id=? OR (session_id IS NULL AND note LIKE ?))",
            (r["id"], r["task"], session, f"%{session}%"))
            if o["session_id"] == session or json.loads(o["note"] or "{}").get("session_id") == session)
        if booked <= 0:
            return usage.cost_usd
        own = bud.estimate_cost(self.p.db, self.cfg, r["provider"], r["model"] or "", {
            "input": usage.input_tokens, "output": usage.output_tokens,
            "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens})
        delta = reported - booked
        if delta >= -1e-6 and delta >= own * SESSION_NET_AGREE - SESSION_NET_SLACK_USD:
            usage.extra["session_cost_usd"] = reported
            usage.cost_usd = round(max(delta, own, 0.0), 6)
        return usage.cost_usd

    def finish_run(self, r: dict, exit_info: dict) -> None:
        db, p = self.p.db, self.p
        run_dir = self._run_dir(r)
        runner.remove_private(run_dir)   # the runner removes them too, unless it died first
        prov = get_provider(r["provider"]).use(r["model"] or "", (self.cfg.get("pricing") or {}).get(r["provider"]))
        usage = prov.parse(run_dir / "output.jsonl", run_dir / "stderr.log")
        self._priced(r, usage)
        self._net_of_session(r, usage)
        stopped = exit_info.get("stopped")
        # A long sleep can drop the CLI's login under a run ("Not logged in" after hours of a closed
        # lid). That run is lost to the sleep; a real log-out shows again on the next run.
        slept_auth = usage.auth_failed and float(exit_info.get("slept_s") or 0) >= SLEPT_LONG_S
        slept = slept_auth or self._slept_during(r, exit_info)
        # A resume that failed on its own and reported no tokens never got going, even if it printed
        # events: it costs nothing, so it ends as the free fallback to a fresh start (_finish_worker).
        # One a host sleep cut is lost to the sleep instead, and keeps its session.
        failed_resume = not stopped and not slept and (exit_info.get("rc") != 0 or usage.error) \
            and not _has_tokens(usage) and _resume_never_started(json.loads(r["note"] or "{}"), usage)
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
        if usage.auth_failed and not slept_auth:
            status = "auth"
        net_lost = status in SLEEP_CUT and bool(NET_LOST_RE.search(usage.error or ""))
        if net_lost:
            self.hold_offline(r["provider"], f"run {r['id']} could not reach its API")
        elif r["provider"] in self._net_holds and (status == "ok" or _has_tokens(usage)):
            self._end_net_hold(r["provider"], f"run {r['id']} reached its API")
        note = json.loads(r["note"] or "{}")
        if usage.session_id:
            note["session_id"] = usage.session_id   # a run the host takes away resumes it (_resumable)
        if "session_cost_usd" in usage.extra:
            note["session_cost_usd"] = usage.extra["session_cost_usd"]   # reported; cost_usd is this run's
        # The runaway guard counts runs that ended without an outcome; a reboot, a host sleep or a
        # hand-off that stands is an outcome, not a loop.
        if status == "lost" and r["boot_id"] and r["boot_id"] != self.boot:
            note.update(not_waste="reboot", lost_to_reboot=self.boot, boot_at=self.boot_at)
        elif status in bud.WASTED and handed_off:
            note["not_waste"] = "handoff"
        elif net_lost:
            # The API host did not resolve: the network went away under the run, not the task.
            note.update(not_waste="network", lost_to_network=True)
            status = "lost"
        elif slept_auth or (status in SLEEP_CUT and slept):
            # A run that overlapped a host sleep did not time out or fail on its own: the host went
            # away under it. It is lost to the sleep, like a run lost to a reboot. A resume cut this
            # way keeps its session (below), so this comes before the fresh-start fallback.
            note.update(not_waste="sleep", lost_to_sleep=True, slept_s=exit_info.get("slept_s"))
            status = "lost"
        elif status == "failed" and _resume_never_started(note, usage):
            note["not_waste"] = "resume"   # nothing ran: the task starts fresh (_finish_worker)
        if status == "lost" and not note.get("session_id") and (note.get("resumes") or {}).get("session"):
            # A resume the sleep or network cut keeps its session for the next resume, not a fresh start.
            note["session_id"] = note["resumes"]["session"]
        source = self._source_for(r)
        # Spend is booked at the run's end, not when the daemon gets to it: a run reaped after
        # downtime must not count toward the current hour. A stamp from the future is clamped.
        ended = min(float(exit_info.get("ended") or time.time()), time.time())
        # The run's end, its spend and what it did to its task commit together: a daemon stopped
        # half way leaves the run "running", and the next tick processes it again from disk.
        with db.tx():
            db.x("UPDATE runs SET ended=?, status=?, exit_code=?, cost_usd=?, cost_estimated=?, input_tokens=?, "
                 "output_tokens=?, cache_read_tokens=?, cache_write_tokens=?, note=?, "
                 "session_id=COALESCE(NULLIF(?,''), session_id) WHERE id=?",
                 (ended, status, exit_info.get("rc"), usage.cost_usd,
                  int(usage.estimated), usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                  usage.cache_write_tokens, json.dumps(note), usage.session_id or "", r["id"]))
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
            if usage.auth_failed and not slept_auth:
                # Logged out is not a task failure and not worth retrying blindly: open the provider's
                # breaker (no run starts on it), say exactly how to fix it, and let check_logins ask
                # its CLI, without a model call, when it is logged in again.
                self.open_breaker(r["provider"], (usage.final_text or usage.error)[:200])
                self.alert(f"auth:{r['provider']}",
                           f"{r['provider']} on {hostname()} is logged out ({(usage.final_text or usage.error)[:120]}). "
                           f"Log in once on that machine ({prov.login_hint}). Nothing starts on it until then; "
                           f"work resumes by itself once a login check passes, and queued work is kept.", "high")
            if r["role"] == "coordinator":
                self._finish_coordinator(r, usage, status, note)
            else:
                self._finish_worker(r, usage, status, run_dir, cut_off if status == "ok" else None,
                                    rebooted=bool(note.get("lost_to_reboot")),
                                    slept=bool(note.get("lost_to_sleep") or note.get("lost_to_network")))
        self._check_price_table(r, usage)
        log(p, f"run {r['id']} end status={status} cost=${usage.cost_usd:.3f}"
               f"{' (estimated)' if usage.estimated else ''} role={r['role']}")

    def _check_price_table(self, r: dict, usage) -> None:
        """Price a finished Claude run's session log with the table that estimates other local
        sessions, and raise a low alert when it drifts from the cost Claude Code reported. A resumed
        run is skipped: its session log also holds the calls of the run it resumed, and so is a run on
        plan windows, where the global cap that table serves never applies."""
        if (r["provider"] != "claude" or usage.estimated or not usage.cost_usd or not usage.session_id
                or float((self.cfg.get("budget") or {}).get("global_daily_usd") or 0) <= 0
                or getattr(self.gates.get("claude"), "regime", "caps") != "caps"   # plan runs never count
                or json.loads(r.get("note") or "{}").get("resumes")):
            return
        try:
            drift = localspend.calibrate(usage.session_id, usage.cost_usd)
        except Exception as e:  # noqa: BLE001 - a price check never breaks finishing a run
            log(self.p, f"price table check failed: {e}")
            return
        if drift is not None and abs(drift) > localspend.DRIFT:
            self.alert("claude_price_table",
                       f"The Claude price table that estimates this machine's other Claude Code sessions is "
                       f"{drift * 100:+.1f}% off the costs Claude Code reported for recent runs, so that "
                       f"estimate is off too. Update the table in tt-project or the account-level pricing.claude setting.",
                       "low", every_s=86400)

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
        checked = note.get("coord_check") or {}
        lost_to = "sleep" if note.get("lost_to_sleep") else "network" if note.get("lost_to_network") else ""
        if (status == "lost" and not r["dir"]) or status == "shutdown" or lost_to:
            # Never launched, ended by `ttp stop --kill`, or cut by a host sleep or a lost network: not
            # a failed turn. Its messages and events stay queued for the next one.
            self._settle_coord_check(checked, f"lost to {lost_to}" if lost_to else status)
            return
        if status == "auth":
            self._settle_coord_check(checked, status)
            return   # its breaker holds the next turn until the login is back; the messages stay queued
        if status != "ok" or not isinstance(actions, list):
            self._settle_coord_check(checked, status, None, [])
            self._coordinator_failed(f"{status} {usage.error[:200]}")
            return
        db.set_kv("coordinator_failures", 0)
        esc = [a for a in actions if isinstance(a, dict) and a.get("type") == "escalate"]
        if esc:
            actions = [a for a in actions if not (isinstance(a, dict) and a.get("type") == "escalate")]
            counts = db.kv(coord.ESCALATIONS_KEY, {}) or {}
            # Once per batch, from a routine turn only, and only when it would raise the effort: the
            # rerun (or any raised turn) decides, so escalation cannot loop.
            batch = [sorted(note.get("events") or []), sorted(note.get("messages") or [])]
            if not note.get("escalated") and not note.get("unblock") and batch != counts.get("batch") and \
                    coord.can_raise_effort(self.cfg, (self.cfg.get("coordinator") or {}).get("tier", "light"),
                                           r.get("effort") or ""):
                why = str(esc[0].get("why") or esc[0].get("reason") or esc[0].get("text") or "")[:300]
                db.set_kv(coord.ESCALATE_KEY, {"run": r.get("id"), "why": why, "ts": time.time(),
                                               **({"due": note["wake_due"]} if note.get("wake_due") else {})})
                db.set_kv(coord.ESCALATIONS_KEY, {**counts, "n": int(counts.get("n", 0)) + 1, "batch": batch})
                log(self.p, f"coordinator turn {r.get('id')} escalated to high effort: {why}")
                # Jev called it routine (if it was asked), and the turn found it was not
                self._settle_coord_check(checked, "escalated", actions, [])
                return   # its messages and events stay queued for the rerun
            db.set_kv(coord.ESCALATIONS_KEY, {**counts, "refused": int(counts.get("refused", 0)) + 1})
            log(self.p, f"coordinator turn {r.get('id')} asked to escalate again; it decides at this effort")
        default_chat = note.get("default_chat")
        problems = coord.apply(self.p, actions, default_chat=default_chat, user_turn=bool(note.get("messages")),
                               turn=r.get("id"), messages=note.get("messages") or [])
        ids = note.get("messages", [])
        if ids:
            db.x(f"UPDATE messages SET handled=1 WHERE id IN ({','.join('?' * len(ids))})", ids)
        evs = note.get("events", [])
        if evs:
            db.x(f"UPDATE events SET status='handled' WHERE id IN ({','.join('?' * len(evs))})", evs)
        self._record_rejections([x[:500] for x in problems])
        self._settle_coord_check(checked, status, actions, problems, float(getattr(usage, "cost_usd", 0) or 0))
        db.set_kv("last_coordinator_summary", {"ts": time.time(), "summary": (out or {}).get("summary", "")})

    def _settle_coord_check(self, checked: dict, status: str, actions: list | None = None,
                            problems: list[str] | None = None, turn_cost: float = 0.0) -> None:
        """Log what a Jev-checked turn did next to its call (coordcheck.settle). Without `problems` the
        turn ended with no result to score (lost, shut down, logged out). Bookkeeping never stops a
        turn from ending."""
        if not checked.get("jev_call"):
            return
        db, cid = self.p.db, int(checked["jev_call"])
        try:
            if problems is None:
                coordcheck.unscored(db, cid, status)
                return
            extra = coordcheck.extra_cost(db, self.cfg, cid, turn_cost) if status == "ok" else 0.0
            coordcheck.settle(db, cid, checked.get("verdict", ""), status, actions, problems, extra_usd=extra)
        except Exception:
            log(self.p, f"coordinator jev check {cid} not settled: "
                + traceback.format_exc().replace("\n", " | ")[:1000])

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
        if rstatus == "done" and task["kind"] == "review" and review_rejects(result):
            # A project's own result rule (`done` plus a verdict): a change that must not proceed
            # is a failed review, so its fix and re-review follow and nothing is approved.
            rstatus, result = "failed", dict(result, status="failed")
        summary = str((result.get("summary") if isinstance(result, dict) else None)
                      or (usage.final_text or usage.error or "")[:1500])
        jobs = _detached_jobs(run_dir)
        if jobs and (rstatus is None and status in ("ok", "timeout", "stalled")
                     or rstatus == "waiting" and not result.get("retry_when")):
            # A job this run detached is still the task's work in flight: the task waits until each
            # job wrote its .rc or is gone (a kill, a reboot) instead of spending an attempt, and the
            # next run is told where they are.
            listing = "; ".join(f"{j['name']}: log {j['log']}, rc {j['rc']}" for j in jobs)
            if rstatus is None:
                summary = (f"run ended ({status}) without a hand-off after detaching jobs ({listing}). "
                           f"Its last message: {summary}")[:1500]
                result = {"status": "waiting", "summary": summary, "retry_after_s": 1800}
                status, rstatus = "ok", "waiting"
            result = {**result, "retry_when": locks.job_probe([j["rc"] for j in jobs], push._own_ttp(self.p)),
                      "waiting_for": result.get("waiting_for") or f"detached jobs: {listing}"[:300]}
        waiting = status == "ok" and rstatus == "waiting"
        # A host reboot is not the task's failure: no attempt, no delay, unless the task keeps being
        # the run the host went down under.
        reboot_lost = status == "lost" and rebooted
        # So is a host sleep, a few times: past max_reboot_losses since the task was last blocked it
        # counts an attempt again, so a task that fails on its own while the host also slept cannot
        # retry for free forever.
        if status == "lost" and slept:
            reboot_lost = self._reboot_losses(task["id"], "lost_to_sleep") + \
                self._reboot_losses(task["id"], "lost_to_network") <= int(
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
        # A review that passes may hand its approved commits to the push queue instead of pushing
        # (delivery.push_queue): it waits as 'pushing' until a batch pushed them (pushq.py).
        approval, push_note, push_quiet = None, "", False
        entries = result.get("push") if isinstance(result, dict) else None
        if new == "done" and entries not in (None, "", []):
            check = (pushq.check_approval(self.p, task, entries, cfg=self.cfg) if task["kind"] == "review"
                     else {"ignored": "only a review task approves pushes"})
            if check.get("ignored"):
                log(self.p, f"task {task['id']}: its push list was ignored: {check['ignored']}")
                push_note = f" Its push list was ignored: {check['ignored']}."
            elif check.get("invalid"):
                why = check["invalid"]
                log(self.p, f"task {task['id']}: push approval invalid: {why}")
                extra["push_invalid"] = int(load_result(task["result"]).get("push_invalid") or 0) + 1
                push_note = f" Push approval invalid: {why}."
                if extra["push_invalid"] < 2:
                    new, not_before, push_quiet = "queued", time.time(), True
                    extra["woke"] = f"push approval invalid: {why}"
                    reason = f"push approval invalid: {why}; its review runs again"
                else:
                    new = "failed"
                    summary = f"push approval invalid again: {why}. {summary}"[:1500]
            else:
                approval, new = check, "pushing"
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
        if approval:
            with db.tx():   # rows, pins and the status together: a failed pin leaves the run to be retried
                pushq.approve(self.p, task["id"], r.get("id"), approval)
                db.update_task(task["id"], **upd)
                # Handled: nothing to decide until the batch reports (pushq.finalize).
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), f"task:{task['id']}", "push_queued", "normal",
                      f"{pushq.queued_text(task, approval)} (run {ended or status}, "
                      f"{'~' if usage.estimated else ''}${usage.cost_usd:.2f})", "handled", task["id"]))
        else:
            db.update_task(task["id"], **upd)
        effort.settle(db, task["id"], new, attempts)
        if new == "done" and task["kind"] == "code":
            self._local_only_due = 0.0   # is its work on a remote? checked this tick
            self._queue_backup(task)
        self._note_dirty_main(task)
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
        try:
            upstream.append(self.p.name, task["id"], fups)   # the user's inbox of notes for tt-project
        except OSError as e:
            log(self.p, f"could not file upstream notes of #{task['id']}: {e}")
        # A plan's findings, plugin advice and follow-up specs are its product: each part gets its own
        # event, sized for the digest to show it whole, so an ordinary hand-off does not grow.
        where = _result_ref(self.p, run_dir if handoff is None else run_dir / RESULT_FILE)
        text = (f"#{task['id']} {task['title']} → {new} (run {ended or status}, {'~' if usage.estimated else ''}"
                f"${usage.cost_usd:.2f}): {_cut(summary, 1200, where)}{push_note}")
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
        quiet = new == "queued" and status in ("limit", "auth", "failed", "lost", "stalled", "no_handoff") or push_quiet
        # A finished code task's next step is its review. A hand-off with nothing else to decide (no
        # follow-ups or notes, normal severity) gets it from the daemon and starts no coordinator turn;
        # any other leaves the review to that turn, so the reviewer starts after it and sees its decisions.
        plain = not fups and not notes and sev == "normal"
        review = self._auto_review(dict(task, **upd), summary, add=plain) \
            if new == "done" and task["kind"] == "code" else None
        if review:
            text += f"\nReview #{review[0]} " + ("queued by the daemon." if review[1] else "was already queued.")
        # A review that failed with fix specs gets its fix and re-review from the daemon, and what waited
        # on it waits on the re-review instead of being blocked. A failure without them still blocks.
        # Upstream notes and deferred follow-ups are not fixes: they stay with the coordinator.
        fixes = [f for f in fups if not upstream.is_note(f) and not f.get("start_after") and not f.get("start_when")]
        fix = self._fix_failed_review(dict(task, **upd), fixes, summary) \
            if new == "failed" and task["kind"] == "review" and fixes and len(fups) <= MAX_FOLLOWUPS else None
        folded = {id(f) for f in fixes} if fix else set()
        if fix:
            moved = ", ".join(f"#{i}" for i in fix[2])
            text += (f"\nFix #{fix[0]} and re-review #{fix[1]} queued by the daemon"
                     + (f"; {moved} now wait on #{fix[1]}." if moved else "."))
        routine = (review is not None and plain) or fix is not None
        if not approval:   # an approval has its push_queued event; the batch's outcome closes the task
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", f"task_{new}", sev, text,
                  "handled" if quiet or routine else "queued", task["id"]))
        if notes:
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "task_notes", "normal",
                  _cut(f"#{task['id']} {task['title']}:{notes}", coord.EVENT_CHARS_BY_KIND["task_notes"], where),
                  "handled" if quiet and len(fups) <= MAX_FOLLOWUPS else "queued", task["id"]))
        for f in fups[:MAX_FOLLOWUPS]:
            # A deferred follow-up names when it may start; the coordinator adds it with those fields.
            start = "; ".join(f"{k}: {str(f[k])[:300]}" for k in ("start_after", "start_when") if f.get(k))
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "followup_proposed", "normal",
                  f"proposed follow-up: {str(f['title'])[:200]}{f' [{start}]' if start else ''} — "
                  f"{_cut(str(f.get('spec', '')), FOLLOWUP_SPEC_CHARS, where)}",
                  "handled" if id(f) in folded else "queued", task["id"]))

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
        self.p.db.set_kv(f"auth_probe:{prov}", 0)   # a login is checked by the next run, not in 15 minutes
        log(self.p, f"{prov} credentials changed; ending the logged-out pause")
        return None

    def _logged_out(self, prov: str) -> bool:
        """Whether `prov`'s auth breaker is open (check_logins closes it)."""
        return alerts.breaker(self.p.db, prov) is not None

    def _may_probe(self, prov: str, now: float) -> bool:
        """While `prov` is logged out, whether one run on it may start now to check the login: only for
        a CLI without a model-free login check, when its pause is over, nothing runs on it, and the
        last check started AUTH_PROBE_S ago or more. Starting every queued task instead would burn
        one failed run each per pause."""
        if not (alerts.breaker(self.p.db, prov) or {}).get("probe"):
            return False   # its CLI's own status check decides (check_logins)
        lim = self._provider_pause(prov)
        if lim and lim.get("until", 0) > now:
            return False
        if self.p.db.one("SELECT id FROM runs WHERE provider=? AND status='running' LIMIT 1", (prov,)):
            return False
        return now >= float(self.p.db.kv(f"auth_probe:{prov}", 0) or 0) + AUTH_PROBE_S

    def open_breaker(self, prov: str, why: str = "", at: float | None = None) -> None:
        """Open `prov`'s auth breaker: no run starts on it (queued tasks keep their attempts) until
        check_logins sees it logged in again. Kept in the project's state, so a restart keeps it."""
        db = self.p.db
        prev = db.kv(alerts.BREAKER + prov) or {}
        if prev.get("open"):
            return
        now = time.time() if at is None else at
        try:
            stamp = get_provider(prov).credentials_stamp()
        except Exception:
            stamp = ""
        # Its CLI said logged in, yet a run was refused again before any got through: that check
        # proves nothing for this logout, or every close would release the queue to fail once more.
        distrust = prev.get("closed_why") == LOGIN_CHECK_PASSED and \
            not alerts.login_proven(db, prov, float(prev.get("closed") or 0))
        db.set_kv(alerts.BREAKER + prov, {"open": True, "opened": now, "checks": 0, "creds": stamp,
                                          "next_check": time.time() + AUTH_CHECK_S[0], "why": why[:200],
                                          **({"distrust": True} if distrust else {})})
        log(self.p, f"{prov}: logged out; no runs start on it until a login check passes")

    def _close_breaker(self, prov: str, rec: dict, why: str) -> None:
        now = time.time()
        self.p.db.set_kv(alerts.BREAKER + prov, {"open": False, "opened": rec.get("opened"), "closed": now,
                                                 "checks": rec.get("checks", 0), "closed_why": why})
        self.p.db.set_kv(f"auth_probe:{prov}", 0)
        log(self.p, f"{prov}: {why}; runs start on it again")

    def check_logins(self) -> None:
        """Close each open auth breaker once its provider is logged in again: its CLI says so (a
        model-free status check on the AUTH_CHECK_S backoff, at once when its credential files
        change), or a run started since it opened got past the login. A CLI without a status check
        says nothing; one run then checks the login every AUTH_PROBE_S (_may_probe)."""
        db, now = self.p.db, time.time()
        # A logged-out alert from before the breaker existed (an upgrade) opens it.
        for ep in db.q("SELECT key, MIN(raised) raised FROM alerts WHERE key LIKE 'auth:%' AND cleared IS NULL "
                       "GROUP BY key"):
            prov = ep["key"].split(":", 1)[1]
            rec = db.kv(alerts.BREAKER + prov) or {}
            if not rec.get("open") and float(rec.get("closed") or 0) < ep["raised"]:
                self.open_breaker(prov, "logged out", at=ep["raised"])
        for row in db.q("SELECT key FROM kv WHERE key LIKE ?", (alerts.BREAKER + "%",)):
            prov = row["key"][len(alerts.BREAKER):]
            rec = alerts.breaker(db, prov)
            if not rec:
                continue
            if alerts.login_proven(db, prov, float(rec.get("opened") or 0)):
                self._close_breaker(prov, rec, "a run on it got past the login")
                continue
            try:
                agent = get_provider(prov)
                stamp = agent.credentials_stamp()
            except Exception:
                agent, stamp = None, ""
            changed = bool(stamp) and stamp != rec.get("creds")
            if now < float(rec.get("next_check") or 0) and not changed:
                continue
            try:
                ok = agent.login_check() if agent and not rec.get("distrust") else None
            except Exception:
                log(self.p, f"{prov}: login check failed\n" + traceback.format_exc())
                ok = None
            if ok or (ok is None and changed):
                self._close_breaker(prov, rec, LOGIN_CHECK_PASSED if ok else "its credentials changed")
                continue
            n = int(rec.get("checks") or 0) + 1
            db.set_kv(alerts.BREAKER + prov, {**rec, "checks": n, "last_check": now, "creds": stamp, "probe": ok is None,
                                              "next_check": now + AUTH_CHECK_S[min(n, len(AUTH_CHECK_S) - 1)]})

    def update_gates(self) -> None:
        provs = {self.cfg.get("core_provider", "claude"), *[t["provider"] for t in self.p.db.q(
            "SELECT DISTINCT provider FROM tasks WHERE provider IS NOT NULL AND status IN ('queued','running')")]}
        # Plan readings count only for the account each provider is logged in as now, so a switch
        # of account drops the old plan's windows before the next run.
        for prov in provs:
            try:
                bud.note_account(self.p.db, prov, get_provider(prov).account())
            except Exception:
                pass
        windows = bud.plan_windows(self.p.db)
        gates, news, red_sent = {}, [], {}
        # After a restart the last levels come from disk, so a change while the daemon was down is news.
        saved = {} if self.gates else (self.p.db.kv("gates") or {})
        for prov in provs:
            g = bud.evaluate(self.p.db, self.cfg, prov, windows)
            lim = self._provider_pause(prov)
            if lim and lim.get("until", 0) > time.time():
                bud._raise(g, "red", f"provider limit: {lim.get('note')}")
                g.max_parallel, g.allow_new_work, g.allow_optional = 0, False, False
            prev = self.gates.get(prov) or _saved_gate(saved.get(prov))
            provider_paused = any(r.startswith("provider limit") for r in g.reasons + (prev.reasons if prev else []))
            # Green, yellow and orange are the budget working as designed (more or fewer workers);
            # only red is news to the user. Its alert clears itself once the gate leaves red.
            first, mark = (self._first_red_in_window(prov, g)
                           if prev and prev.level != "red" and g.level == "red" and not provider_paused
                           else (False, None))   # a pause has its own alert
            if mark is not None:
                red_sent[prov] = mark
            if first:
                capped = any("cap reached" in r for r in g.reasons)
                hint = "New work is paused; running work finishes and replies to you continue. " + (
                    "It starts again by itself when the budget day resets. "
                    if any(r.startswith("global daily cap") for r in g.reasons) else "") + (
                    "You can raise the cap (carefully) by telling me, or in the web app."
                    if capped else "The web app's Budget tab shows what spent it.")
                news.append((f"Budget for {prov} is now red: {'; '.join(g.reasons)}. " + hint,
                             "high", f"budget:{prov}"))
            gates[prov] = g
        # The global cap counts only for providers billed by usage: a project whose providers are all
        # on plan windows needs no other machines' totals and no scan of local sessions.
        if any(g.regime == "caps" for g in gates.values()):
            gcap.refresh_async(self.cfg.get("budget") or {})   # other machines' totals, in the background
            localspend.scan_async(self.cfg.get("budget") or {})   # other local Claude Code sessions
        # One transaction: saved gates without their alert would hide the change from every later
        # tick and restart. Gates first: a relay reading an alert before the gates show red would
        # count it as cleared.
        with self.p.db.tx():
            self.p.db.set_kv("gates", {k: v.as_dict() for k, v in gates.items()})
            if red_sent:   # the window's marker commits with its alert, or neither does
                self.p.db.set_kv("budget_red_sent",
                                 {**(self.p.db.kv("budget_red_sent", {}) or {}), **red_sent})
            for text, sev, ref in news:
                self.p.db.post("out", text, chat=None, kind="alert", severity=sev, ref=ref)
        self.gates = gates

    def _first_red_in_window(self, prov: str, g: bud.Gate) -> tuple[bool, dict | None]:
        """Whether a red gate at a plan line is the first in that plan window, and the provider's
        new budget_red_sent entry (or None) for the caller to save in the alert's transaction. The
        line holds until the window resets, so one alert per window is enough. Red for any other
        reason (a dollar cap, the runaway guard) is always news."""
        n = g.numbers or {}
        line = float(n.get("limit") or 100)
        at_line = [r for r in n.get("plan") or [] if r.get("resets_at") and r["utilization"] >= line]
        if not at_line:
            return True, None
        sent = self.p.db.kv("budget_red_sent", {}) or {}
        mine = {w: t for w, t in (sent.get(prov) or {}).items() if float(t) > time.time()}
        # A window's reset time may move by seconds between readings; the next window's is hours on.
        if all(abs(float(mine.get(r["window"], 0)) - float(r["resets_at"])) < 600 for r in at_line):
            return False, None
        mine.update({r["window"]: r["resets_at"] for r in at_line})
        return True, mine

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
        if not text:
            # Nothing to report: what this watcher reported before is over. A recurrence reopens it.
            scr.close_watcher_issues(self.p.db, f"watcher:{s['name']}", why=scr.CLEAN_RUN_WHY)
            return "ok (0 observations)"
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
        jev_lines = jevuse.lines(db, self.cfg) if payload.get("jev_report", s["name"] == "daily-review") else []
        if jev_lines:
            spec += (f"\n\nJev uses over the last {jevuse.window_s(self.cfg) / 86400:g} d (calls, cost, estimated "
                     f"savings, net, error rate; a use with no net saving is switched off by itself):\n"
                     + "\n".join(f"- {line}" for line in jev_lines))
        if payload.get("unblock_report", s["name"] == "daily-review"):
            try:
                spec += "\n\nUnblocking quality:\n" + "\n".join(f"- {line}" for line in unblock.lines(db))
            except Exception as e:   # a metric must not keep the review from starting
                log(self.p, f"unblocking metrics failed: {type(e).__name__}: {e}")
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
        v = scr.screen(self.p.db, self.cfg, source, text, hint, jev=self.jev, **again)
        if v.jev_out_of_funds:
            self.alert("jev-funds", JEV_FUNDS_TEXT, "high")
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
        queued = db.q("SELECT id, ts, kind, severity FROM events WHERE status='queued' ORDER BY id")
        evs = queued[:int(c.get("max_events_per_turn", 40))]
        wake: dict = {}
        w: dict | None = None
        # A routine turn that found its batch harder than it looked: rerun it now, once, at high effort.
        esc = db.kv(coord.ESCALATE_KEY) or {}
        if esc:
            w = {"due": esc.get("due")} if esc.get("due") else None
        elif not msgs and not evs:
            last = float(db.kv("last_coordinator_turn", 0))
            if now - last <= min(float(c.get("idle_wake_s", 3600)), float(c.get("starve_wake_s", 300))):
                return
            w = idle_wake(self.p, self.cfg, {k: g.as_dict() for k, g in self.gates.items()}, now)
            if not w["due"]:
                return
            if w["due"] == "starve":
                db.set_kv("starve", w["starve"])
            wake = w["wake"]
        else:
            newest = max([m["ts"] for m in msgs] + [e["ts"] for e in evs])
            oldest = min([m["ts"] for m in msgs] + [e["ts"] for e in evs])
            debounce = float(c.get("debounce_s", 15))
            if now - newest < debounce and now - oldest < 4 * debounce:
                return
            if not msgs and self._batch_hold(queued, now):
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
        logged_out = self._logged_out(provider)
        if logged_out and not self._may_probe(provider, now):
            return   # logged out: one run at a time checks the login, and this turn is not it
        check = None
        try:
            prompt = coord.digest(self.p, gates, [e["id"] for e in evs], [m["id"] for m in msgs])
            due = (w or {}).get("due")
            triggers, seen = coord.effort_triggers(db, self.cfg, [e["id"] for e in evs], due,
                                                   [m["id"] for m in msgs], gates, now)
            if esc:
                triggers = [f"escalated: {str(esc.get('why') or '')[:200]}".rstrip(": "), *triggers]
            can_raise = coord.can_raise_effort(self.cfg, c.get("tier", "light"))
            # Jev rates only a turn the rules leave routine and that a raise would change.
            check = self._coord_check(provider, c.get("tier", "light"), [e["id"] for e in evs], due) \
                if can_raise and not triggers else None
            if check and check["verdict"] == "needs_thought":
                triggers.append(f"jev: needs thought ({check['reason']})")
            checked = {"coord_check": {"jev_call": check["jev_call"], "verdict": check["verdict"]}} if check else {}
            unblock = ", ".join(triggers)
            raised = bool(unblock) and can_raise
            prompt += ("\n\nThis turn's effort: raised (" + unblock[:300] + ")." if raised else
                       "\n\nThis turn's effort: routine." + (" If this batch is harder than routine bookkeeping, "
                       "return only an `escalate` action: it reruns once at high effort." if can_raise else ""))
            head, context = coord.prompt_parts(self.p)
            run_id = self.start_run("coordinator", prompt, provider, c.get("tier", "light"), str(self.p.base),
                           read_only=True, schema=coord.ACTIONS_SCHEMA, system=head, context=context,
                           cache_ttl=str(c.get("cache_ttl", "1h") or ""),
                           budget_usd=float(c.get("turn_budget_usd", 1.0)),
                           timeout_s=float(c.get("turn_timeout_s", 600)),
                           note={"messages": [m["id"] for m in msgs], "events": [e["id"] for e in evs],
                                 "default_chat": default_chat, **({"unblock": unblock} if unblock else {}),
                                 "triggers": triggers, **({"wake_due": due} if due else {}),
                                 **({"escalated": True} if esc else {}), **checked},
                           unblock=unblock)
        except Exception as e:
            # A turn that cannot even start backs off like a failed turn instead of retrying every tick.
            log(self.p, "coordinator start failed: " + traceback.format_exc().replace("\n", " | ")[:2000])
            if check:
                self._settle_coord_check({"jev_call": check["jev_call"]}, "not started")
            self._coordinator_failed(f"could not start: {type(e).__name__}: {e}"[:250])
            return
        if check:
            try:
                jevuse.set_ref(db, check["jev_call"], f"run:{run_id}")
            except Exception:   # bookkeeping: the turn has started either way
                log(self.p, "coordinator jev check ref not set: "
                    + traceback.format_exc().replace("\n", " | ")[:1000])
        db.set_kv("last_coordinator_turn", now)
        db.set_kv(coord.EFFORT_SEEN_KEY, seen)
        if esc:
            db.x("DELETE FROM kv WHERE key=?", (coord.ESCALATE_KEY,))
        else:
            db.set_kv("idle_wake", wake)
        if logged_out:
            db.set_kv(f"auth_probe:{provider}", now)

    def _batch_hold(self, evs: list[dict], now: float) -> bool:
        """Routine events (a task done, its follow-ups and notes, normal observations) wait up to
        coordinator.batch_s so one turn reads several, but only while no worker slot would sit idle
        for it: every slot is busy, or runnable queued work is there to fill each free one."""
        batch = float(self.cfg["coordinator"].get("batch_s", 300))
        if batch <= 0 or not evs or now - min(e["ts"] for e in evs) >= batch:
            return False
        if not all(coord.batchable(e["kind"], e["severity"]) for e in evs):
            return False
        db = self.p.db
        gate = self.gates.get(self.cfg.get("core_provider", "claude"))
        busy = db.one("SELECT COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running'")["n"]
        slots = (gate.max_parallel if gate.allow_new_work else 0) if gate else \
            int(self.cfg["budget"].get("max_parallel_workers", 6))
        if busy >= slots:
            return True   # no slot is free; nothing the turn queues could start before one is
        paused = db.paused_resources()
        runnable = sum(1 for t in db.ready_tasks() if not coord.task_resources(t) & paused.keys()
                       and not (t["blocked_reason"] or "").startswith((PAUSED_NOTE, LOGGED_OUT_NOTE, NET_HELD_NOTE)))
        return busy + runnable >= slots

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
        now, core = time.time(), self.cfg.get("core_provider", "claude")
        logged_out = {prov: self._logged_out(prov) for prov in {t["provider"] or core for t in ready}}
        # On a logged-out provider the cheapest task goes first: it is the one run that checks the login.
        out = [t for t in ready if logged_out[t["provider"] or core]]
        if out:
            rank = {t: i for i, t in enumerate(bud.TIER_ORDER)}
            ready = [t for t in ready if not logged_out[t["provider"] or core]] + \
                sorted(out, key=lambda t: rank.get(t["tier"], len(rank)))
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
            provider = task["provider"] or core
            if logged_out[provider] and not self._may_probe(provider, now):
                # Logged out: the queue keeps its tasks, attempts untouched, until a run succeeds.
                held = (f"{LOGGED_OUT_NOTE} ({provider}); it starts once a login check passes")
                if note != held:
                    db.update_task(task["id"], blocked_reason=held)
                continue
            if self.net_held(provider) and not self._net_may_probe(provider):
                # Its API host does not resolve: the task waits, attempts untouched.
                held = f"{NET_HELD_NOTE} waiting for {provider}'s API host to resolve"
                if note != held and (not note or note.startswith(NET_HELD_NOTE)):
                    db.update_task(task["id"], blocked_reason=held)
                continue
            if note.startswith((PAUSED_NOTE, LOGGED_OUT_NOTE, NET_HELD_NOTE)):
                db.update_task(task["id"], blocked_reason=None)
            gate = self.gates.get(provider) or bud.evaluate(db, self.cfg, provider, bud.plan_windows(db))
            if not gate.allow_new_work or busy.get(provider, 0) >= gate.max_parallel:
                continue
            if task["origin"] in ("schedule", "harness") and not gate.allow_optional:
                continue
            if needs_device(task, self.cfg) and self._device_tasks_running() >= self._device_max_tasks():
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
            picked = self._pick_effort(task, provider)
            if picked:
                task = dict(task, tier=picked["tier"])
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
                if picked:
                    note["pick"] = picked
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
            if logged_out[provider]:
                db.set_kv(f"auth_probe:{provider}", now)   # the one check of the login until the next
            self._progress()   # each start may have added a worktree
            busy[provider] = busy.get(provider, 0) + 1
            if gate.regime == "caps":
                committed += cost
        # A task a gate kept out this tick cannot start, so it must not hold `ttp lock` commands off.
        for task in ready:
            if task["id"] not in reached:
                self._unreserve(task)

    def _pick_effort(self, task: dict, provider: str) -> dict | None:
        """The task's tier for this start (see effort.pick), stored on the task; None leaves it as it is."""
        try:
            picked = effort.pick(self.p.db, self.cfg, task, provider, jev=self.jev)
        except Exception as e:   # picking must never hold a task back
            log(self.p, f"task {task['id']}: effort not picked: {e}")
            return None
        if not picked:
            return None
        if picked["tier"] != task["tier"]:
            log(self.p, f"task {task['id']}: tier {task['tier']} -> {picked['tier']} ({picked['by']})")
            marked = effort.mark_raised(task) if picked["by"] == "retry" else None
            self.p.db.update_task(task["id"], tier=picked["tier"], **({"result": marked} if marked else {}))
        return picked

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
        # A resume cut short before it did anything carries the work of the runs it continued.
        cost, took, prev, seen = 0.0, 0.0, r, set()
        while prev and prev["id"] not in seen:
            seen.add(prev["id"])
            cost += float(prev["cost_usd"] or 0)
            took += float(prev["ended"] or 0) - float(prev["started"] or 0)
            back = (json.loads(prev["note"] or "{}").get("resumes") or {}).get("run")
            prev = self.p.db.one("SELECT * FROM runs WHERE id=?", (back,)) if back else None
        if cost < float(want.get("min_usd", 0.5)) and took < float(want.get("min_s", 600)):
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
            locks.unreserve(self._reserve_path(res), shared.holder(self.p, res, f"task #{task['id']}", self.cfg))

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
        once free space is back at `disk.resume_free_gb` (never below the threshold), or, with that
        unset or at least the disk's size, above DISK_RESUME times the threshold, so a disk hovering
        at the line does not flap.
        The episode is kept in the database: a restart neither re-alerts nor forgets it."""
        cfg = self.cfg.get("disk", {})
        pct, gb = float(cfg.get("min_free_pct", 5) or 0), float(cfg.get("min_free_gb", 150) or 0)
        try:
            mine = machines.disk_min_free_gb()   # this machine's entry in the machines list wins
        except Exception:
            mine = None
        if mine:
            gb = mine[1]
        # A machine's own threshold keeps the DISK_RESUME rule: the project's resume point was set
        # against its own threshold and could hold a shared disk that others keep near full forever.
        resume_gb = None if mine else disk_resume_gb(cfg)
        worst = None   # (margin, path, free, total, threshold, resume point)
        for path in {self.p.base.resolve(), self.p.worktrees.resolve()}:
            try:
                u = shutil.disk_usage(path)
            except OSError:
                continue
            need = min(pct / 100 * u.total, gb * 1e9)
            resume = need * DISK_RESUME if resume_gb is None else max(need, resume_gb * 1e9)
            if resume >= u.total:   # a resume point the disk can never reach would hold it forever
                resume = need * DISK_RESUME
            margin = u.free - (resume if self._disk_low else need)
            if worst is None or margin < worst[0]:
                worst = (margin, path, u.free, u.total, need, resume)
        if worst is None:
            return
        _, path, free, total, need, resume = worst
        self._disk_free = free
        low = need > 0 and worst[0] < 0
        now = time.time()
        db = self.p.db
        info = {"path": str(path), "free_gb": round(free / 1e9, 1), "total_gb": round(total / 1e9, 1),
                "threshold_gb": round(need / 1e9, 1), "resume_gb": round(resume / 1e9, 1), "low": low,
                "checked": now, **({"machine": mine[0]} if mine else {})}
        last = db.kv("disk") or {}
        if (low != last.get("low") or abs(info["free_gb"] - float(last.get("free_gb") or 0)) >= 1
                or now - float(last.get("checked") or 0) > 600):
            db.set_kv("disk", info)
        if low == self._disk_low:
            return
        self._disk_low = low
        if low:
            usage = disk_usage_line(disk_breakdown(self.p, path), total - free)
            db.set_kv("disk_low", {"path": str(path), "free_gb": info["free_gb"], "threshold_gb": info["threshold_gb"],
                                   "since": now, "usage": usage})
            log(self.p, f"disk low: {free / 1e9:.1f} GB free under {path} (guard {need / 1e9:.1f} GB); "
                        f"only questions and plans start. {usage}")
            source = f"machine {mine[0]}'s min_free_gb" if mine else "disk.min_free_gb"
            self.alert("disk", f"Only {free / 1e9:.1f} GB free under {path} (guard: {need / 1e9:.0f} GB, the smaller "
                               f"of {pct:g}% of the disk and {gb:g} GB from {source}). {usage} New tasks other than "
                               f"questions and plans are held until {resume / 1e9:.0f} GB are free; running "
                               f"work, questions, plans and replies continue. Finished tasks' worktrees are removed as "
                               f"they end; `ttp prune {self.p.name}` sweeps now and lists the ones kept. A shared disk "
                               f"that others keep near full by design takes its own threshold: `ttp machines add "
                               f"<alias> --min-free-gb N`.", "high", every_s=0)
        else:
            db.set_kv("disk_low", None)
            log(self.p, f"disk space ok again: {free / 1e9:.1f} GB free under {path}")

    def lint_charter(self) -> None:
        """Flag dated charter sections that contradict a standing restriction (coord.charter_lint);
        a stat call per tick, the scan only when the charter changed."""
        try:
            coord.charter_lint(self.p)
        except Exception:
            log(self.p, "charter lint: " + traceback.format_exc().replace("\n", " | ")[:1000])

    def check_integrity(self, start: bool = False) -> None:
        """On the first start of a new boot, check the harness for damage a power cut left and repair
        what its last commit can (see ttp.integrity); also report unfinished code tasks' worktrees
        whose `git status` fails. While something stays broken, check again at each start and
        hourly. Repairs are a note in the feed; what stays broken is one alert keyed `integrity`."""
        db = self.p.db
        last = db.kv(KV_INTEGRITY) or {}
        broken = bool(last.get("bad") or last.get("worktrees"))
        if start:
            due = last.get("boot") != self.boot or broken
        else:   # a tick: only the hourly re-check of something still broken
            due = broken and time.time() - float(last.get("at") or 0) >= INTEGRITY_RECHECK_S
        if not due:
            return
        try:
            ids = [r["id"] for r in db.q("SELECT id FROM tasks WHERE kind='code' AND status NOT IN "
                                         f"({','.join('?' * len(TERMINAL_TASK_STATES))})", TERMINAL_TASK_STATES)]
            res = integrity.check(self.p, [(t, self.p.worktrees / f"t{t}") for t in ids])
        except Exception:
            log(self.p, "integrity check: " + traceback.format_exc().replace("\n", " | ")[:1000])
            return
        res.update(boot=self.boot, at=time.time())
        db.set_kv(KV_INTEGRITY, res)
        log(self.p, f"integrity check in {res['seconds']} s: fsck {res['fsck']}, {len(res['restored'])} restored, "
                    f"{len(res['bad'])} unrepaired, {len(res['worktrees'])} broken worktree(s)")
        if res["restored"]:
            db.post("out", "The harness check after a restart repaired files a cut write had damaged: "
                           + "; ".join(res["restored"])[:2000], kind="info", severity="low")
        text = integrity.problem_text(res)
        if text:
            self.alert("integrity", text, severity="high", every_s=86400)

    def check_release(self) -> None:
        """Hourly (and at start): is a newer tt-project installed than this harness runs? Status and
        the web app say so while it is. With upgrade.auto on, and no push or upgrade in flight, start
        this project's own `ttp upgrade` once per strictly newer release; it restarts this daemon,
        keeping workers. The same version from another commit is only shown."""
        now = time.time()
        if now < self._release_due:
            return
        self._release_due = now + release.CHECK_S
        db = self.p.db
        release.guard_harness(self.p.harness)    # harnesses made before the guard get it without an upgrade
        self.check_older_release()   # first: drift must see a lib/current it restored
        try:
            d = release.drift(self.p)
        except Exception as e:   # a half-written install must not stop the tick
            log(self.p, f"release check failed: {type(e).__name__}: {e}")
            return
        if d != db.kv(release.KV_RELEASE):
            db.set_kv(release.KV_RELEASE, d)
            if d:
                log(self.p, f"tt-project {d['installed']} installed; harness on {d['current']}")
        if not d or not d.get("newer") or not (self.cfg.get("upgrade") or {}).get("auto", True) \
                or db.kv("paused", False):
            return
        why = release.hold_reason(self.p, d)
        if why:
            if why.endswith("in flight"):
                self._release_due = now + release.HELD_RECHECK_S
            if (db.kv(release.KV_AUTO) or {}).get("key") != d["key"] or why.endswith("in flight"):
                log(self.p, f"automatic upgrade to {d['installed']} held: {why}")
            return
        log(self.p, f"automatic upgrade from {d['current']} to {d['installed']}: starting `ttp upgrade`")
        self._release_due = now + release.HELD_RECHECK_S   # an upgrade that finds a push at its swap retries soon
        try:
            release.start(self.p, d)
        except Exception as e:
            release.finish(self.p, "failed", why=f"{type(e).__name__}: {str(e)[:200]}")
            log(self.p, f"automatic upgrade did not start: {type(e).__name__}: {e}")

    def check_older_release(self) -> None:
        """~/.tt-project/lib/current holds an older version than this harness runs: an older plugin's
        `ttp setup` replaced the newer install, so `ttp` on PATH, remote shipping and automatic
        upgrades all work from the older release. When a complete release at the harness version or
        newer is still in ~/.tt-project/lib, point lib/current back at it and say so (low). Otherwise,
        or when `ttp setup --force` made the downgrade on purpose, one alert that clears by itself."""
        db = self.p.db
        try:
            o = release.older(self.p)
            back = (release.restorable(o["harness"]) if o and not o.get("forced")
                    and release.installed().is_symlink() else None)
            if back:
                release.point_current(back)
                log(self.p, f"installed tt-project {o['installed']} was older than this harness "
                            f"({o['harness']}): lib/current now points at {back}")
                self.alert("release-restored",
                           f"An older tt-project ({o['installed']}) had replaced the installed release; "
                           f"lib/current points at {back.name} again, so `ttp` on PATH and automatic "
                           f"upgrades use it.", severity="low", every_s=86400)
                o = release.older(self.p)
        except Exception as e:
            log(self.p, f"installed-release check failed: {type(e).__name__}: {e}")
            return
        if o == db.kv("release_older"):
            return
        db.set_kv("release_older", o)
        if not o:
            log(self.p, "the installed tt-project is no longer older than this harness")
            return
        log(self.p, f"installed tt-project {o['installed']} is older than this harness ({o['harness']})")
        why = ("`ttp setup --force` installed it on purpose, so it is left as is"
               if o.get("forced") else
               f"an older plugin's `ttp setup` replaced a newer install, and ~/.tt-project/lib holds no "
               f"{o['harness']} or newer to point back at")
        self.alert("release-older", f"The installed tt-project ({o['installed']}) is older than this harness "
                                    f"({o['harness']}): {why}. `ttp` on PATH and automatic upgrades use the "
                                    f"older release until `ttp setup` runs from a plugin at {o['harness']} or "
                                    f"newer; this alert clears by itself then.", "high", every_s=0)

    def check_local_only(self) -> None:
        """A done code task may leave the only copy of its work on a local branch. Hourly, and in the
        tick a code task hands off done, fetch and look for the branches of code tasks done in the
        last LOCAL_ONLY_DAYS that hold work no remote has (worktree.local_only: neither the head nor,
        rebased, amended or batched by a reviewer, its changes are on a remote). Left alone: a task
        an unfinished task still needs (a queued review or a fix, see worktree.needed_by), one a done
        review names (its id, branch or head commit), and one that finished before this check first
        ran (KV_LOCAL_ONLY_FROM), so an upgrade posts no burst for old work. Each newly found one
        posts one coordinator event; status and the web app count it until the work is pushed or
        merged, the task leaves done (cancelled) or falls out of the window. Nothing is pushed. A
        repository without a remote is skipped quietly. After a failed fetch the remote refs may be
        old: flags may clear, but no new one is raised. The fetch and the git comparisons run in a
        thread and a later tick applies what they found: in a large repository they can take minutes,
        which held the tick and delayed every other part of it. While a check runs none starts; a
        check that fails, in the thread or before it, is logged and the next one starts when due."""
        job = self._local_only_job
        if job is not None:
            if job["done"].is_set():
                self._local_only_job = None
                self._apply_local_only(job)
            return
        now = time.time()
        if now < self._local_only_due:
            return
        self._local_only_due = now + LOCAL_ONLY_EVERY_S
        db = self.p.db
        since = db.kv(KV_LOCAL_ONLY_FROM)
        if not isinstance(since, (int, float)):
            since = now
            db.set_kv(KV_LOCAL_ONLY_FROM, since)
        told = db.kv(KV_LOCAL_ONLY) or {}
        tasks = db.q("SELECT id, title, branch, updated FROM tasks WHERE kind='code' AND status='done' "
                     "AND branch IS NOT NULL AND branch!='' AND updated>=?",
                     (max(since, now - LOCAL_ONLY_DAYS * 86400),))
        reviews = []
        if tasks:
            open_tasks = db.q("SELECT id, status, spec, depends_on, labels FROM tasks WHERE status NOT IN (%s)"
                              % ",".join("?" * len(TERMINAL_TASK_STATES)), TERMINAL_TASK_STATES)
            reviews = db.q("SELECT id, status, spec, depends_on, labels FROM tasks WHERE kind='review' "
                           "AND status='done' AND updated>=?", (now - 2 * LOCAL_ONLY_DAYS * 86400,))
            tasks = [t for t in tasks if not worktree.needed_by(t, open_tasks) and not worktree.needed_by(t, reviews)]
            backing = db.kv(KV_BACKUP) or {}   # a backup push still to come decides first
            tasks = [t for t in tasks if str(t["id"]) not in backing]
        if not tasks and not told:
            return
        job = {"tasks": tasks, "reviews": reviews, "found": ({}, True, {}), "error": None, "done": threading.Event()}
        if not tasks:
            job["done"].set()
            return self._apply_local_only(job)
        try:
            branches, targets, known = [t["branch"] for t in tasks], [worktree.base_ref(self.p)], set(self._local_only_ok.items())
            pushed = pushq.pushed_heads(self.p.db)
        except Exception:
            log(self.p, "local-only branch check: " + traceback.format_exc().replace("\n", " | ")[:1000])
            return

        def work() -> None:
            try:
                job["found"] = worktree.local_only(self.p.root, branches, targets=targets, known=known, pushed=pushed)
            except Exception:
                job["error"] = traceback.format_exc().replace("\n", " | ")[:1000]
            finally:
                job["done"].set()
        self._local_only_job = job
        threading.Thread(target=work, daemon=True).start()

    def _apply_local_only(self, job: dict) -> None:
        """Record what a finished check_local_only found: flag the new local-only branches, clear the rest."""
        if job["error"]:
            log(self.p, "local-only branch check: " + job["error"])
            return
        db, now, tasks, reviews, found = self.p.db, time.time(), job["tasks"], job["reviews"], job["found"]
        told = db.kv(KV_LOCAL_ONLY) or {}
        if found is None:
            if told:
                db.set_kv(KV_LOCAL_ONLY, None)
            return
        heads, fetched, clean = found
        self._local_only_ok.update(clean)
        live = {}
        with db.tx():
            for t in tasks:
                key, b = str(t["id"]), t["branch"]
                if b not in heads or not fetched and key not in told:
                    continue
                head, ahead = heads[b]
                if _names_commit(reviews, head):
                    continue
                live[key] = {"branch": b, "head": head, "ahead": ahead, "since": (told.get(key) or {}).get("since", now)}
                if key not in told:
                    log(self.p, f"task {t['id']}: branch {b} exists only on this machine ({ahead} commits ahead)")
                    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                         (now, f"task:{t['id']}", "local_only", "normal",
                          f"task #{t['id']}'s branch {b} exists only on this machine, {ahead} commit"
                          f"{'' if ahead == 1 else 's'} ahead of every remote ({t['title']}). Nothing pushes it "
                          f"automatically: deliver it as the charter allows, or cancel the task if the work is "
                          f"not wanted.", "queued", t["id"]))
            if live != told:
                db.set_kv(KV_LOCAL_ONLY, live or None)

    def _queue_backup(self, task: dict) -> None:
        """With delivery.backup_remote set, line a done code task's branch up for backup_branches.
        Off (the default), nothing is queued and nothing is pushed."""
        d = self.cfg.get("delivery") or {}
        branch = str(task.get("branch") or "")
        if not str(d.get("backup_remote") or "").strip() or not branch:
            return
        db = self.p.db
        pending = db.kv(KV_BACKUP) or {}
        pending[str(task["id"])] = {"branch": branch, "tries": 0, "next": 0}
        db.set_kv(KV_BACKUP, pending)

    def backup_branches(self) -> None:
        """Push each done code task's own branch to `delivery.backup_remote` (a git remote; off when
        unset), so work that is on no remote yet has a copy off this machine. Fast-forward only, under
        the same name: never with force, never to the push branch, main or the base_ref (push.backup).
        A push that is not a fast-forward is skipped with one observation; one that fails is tried
        again later, BACKUP_TRIES times in all. The queue (KV_BACKUP) survives restarts; the pushes run
        in a thread and a later tick applies what they did. Turning the key off or forbidding pushes
        (delivery.push_allowed) drops the queue without pushing. The local-only check leaves a queued
        branch alone, and looks again once its backup ended."""
        job = self._backup_job
        if job is not None:
            if job["done"].is_set():
                self._backup_job = None
                self._apply_backup(job)
            return
        db = self.p.db
        pending = db.kv(KV_BACKUP) or {}
        if not pending:
            return
        d = self.cfg.get("delivery") or {}
        remote = str(d.get("backup_remote") or "").strip()
        why = push.backup_problem(d) or ("" if push_allowed(d) else "delivery.push_allowed is off")
        if not remote or why:
            if why:
                log(self.p, f"backup of {len(pending)} task branch(es) dropped: {why}")
            db.set_kv(KV_BACKUP, None)
            self._local_only_due = 0.0
            return
        now = time.time()
        due = {k: v for k, v in pending.items() if float(v.get("next") or 0) <= now}
        if not due:
            return
        job = {"remote": remote, "due": due, "out": {}, "done": threading.Event()}
        root = self.p.root

        def work() -> None:
            for k, v in due.items():
                try:
                    job["out"][k] = push.backup(root, remote, v["branch"])
                except Exception:
                    job["out"][k] = ("failed", traceback.format_exc().replace("\n", " | ")[-300:])
            job["done"].set()
        self._backup_job = job
        threading.Thread(target=work, daemon=True).start()

    def _apply_backup(self, job: dict) -> None:
        """Record what a finished backup_branches pass did: drop pushed, skipped and refused branches
        from the queue (the last two with an observation), and schedule failed ones' next try."""
        db, now, remote = self.p.db, time.time(), job["remote"]
        with db.tx():
            pending = db.kv(KV_BACKUP) or {}
            for k, (outcome, detail) in job["out"].items():
                v = pending.get(k)
                if v is None or v.get("branch") != job["due"][k]["branch"]:
                    continue   # dropped or re-queued meanwhile
                b = v["branch"]
                if outcome == "failed" and v.get("tries", 0) + 1 < BACKUP_TRIES:
                    v["tries"] = v.get("tries", 0) + 1
                    v["next"] = now + BACKUP_RETRY_S * v["tries"]
                    log(self.p, f"task {k}: backup of {b} to {remote} failed (try {v['tries']}): {detail}")
                    continue
                del pending[k]
                if outcome == "pushed":
                    self._local_only_ok[b] = detail
                    log(self.p, f"task {k}: branch {b} backed up to {remote} at {detail[:10]}")
                    continue
                log(self.p, f"task {k}: branch {b} not backed up to {remote} ({outcome}): {detail}")
                if outcome == "gone":
                    continue
                why = {"not_ff": "it is not a fast-forward, so it was skipped and nothing was forced",
                       "refused": "the backup refused it",
                       "failed": f"the push failed {BACKUP_TRIES} times"}[outcome]
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (now, f"task:{k}", "observation", "normal",
                      f"task #{k}'s branch {b} was not backed up to {remote} (delivery.backup_remote): {why} "
                      f"({detail}).", "queued", int(k)))
            db.set_kv(KV_BACKUP, pending or None)
        self._local_only_due = 0.0   # what is still only here is flagged now

    def _note_dirty_main(self, task: dict) -> None:
        """At a hand-off, record one observation when the project's main checkout has uncommitted
        changes to tracked paths: work there is on no branch and no remote. Once per set of paths
        (KV_DIRTY_MAIN); a clean checkout resets it. Nothing is committed or changed."""
        paths = worktree.dirty_tracked(self.p.root)
        if paths is None:
            return
        db = self.p.db
        key = hashlib.sha256("\0".join(paths).encode()).hexdigest()[:16] if paths else None
        if key == db.kv(KV_DIRTY_MAIN):
            return
        db.set_kv(KV_DIRTY_MAIN, key)
        if not paths:
            return
        shown = ", ".join(paths[:20]) + (f" and {len(paths) - 20} more" if len(paths) > 20 else "")
        log(self.p, f"main checkout has uncommitted changes to {len(paths)} tracked path(s)")
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "daemon", "observation", "low",
              f"The project's main checkout has uncommitted changes to {len(paths)} tracked path"
              f"{'' if len(paths) == 1 else 's'}, seen at #{task['id']}'s hand-off: {shown}. They are on no "
              f"branch and no remote. Nothing was committed or changed.", "queued", task["id"]))

    def read_upstream(self) -> None:
        """With `upstream.ingest` on, new notes in the user's upstream inboxes become coordinator events
        (this machine's every minute, remote ones at most hourly); otherwise only notes addressed to
        this project (`ttp note --to`) in this machine's inbox are."""
        now = time.time()
        if now - self._upstream_checked < upstream.LOCAL_EVERY_S:
            return
        self._upstream_checked = now
        try:
            n = upstream.ingest(self.p, self.cfg, now)
        except (OSError, ValueError) as e:
            log(self.p, f"reading the upstream inbox failed: {type(e).__name__}: {e}")
            return
        if n:
            log(self.p, f"{n} new upstream note(s) for the coordinator")

    def forward_upstream(self) -> None:
        """Send this machine's new upstream notes on to the machines that cannot reach it (upstream.forward),
        in a thread: a slow or hung ssh never holds up the tick. One pass at a time per daemon; the
        daemons of this user's other projects skip a pass while one runs."""
        now = time.time()
        if now - self._forwarded < upstream.FORWARD_EVERY_S or (self._forwarder and self._forwarder.is_alive()):
            return
        self._forwarded = now

        def work():
            try:
                n = upstream.forward(now)
            except (OSError, ValueError) as e:
                log(self.p, f"sending upstream notes on failed: {type(e).__name__}: {e}")
                return
            if n:
                log(self.p, f"{n} upstream note(s) sent on to other machines")
        self._forwarder = threading.Thread(target=work, daemon=True, name="upstream-forward")
        self._forwarder.start()

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
                      f"(blocking access) only if the charter allows no alternative."
                      if name in known else
                      f"Resource {text}. It is a lock, not a machine, so there is nothing to route around: "
                      f"find why its tasks fail and fix that.", "queued"))
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
        db = self.p.db
        logged = db.kv(KV_WORKTREES_LOGGED) or {}      # task -> the keep reason last logged
        dirty = db.kv(KV_WORKTREES_DIRTY) or {}        # task -> its kept tracked changes
        for r in worktree.sweep(self.p, older_than_s=days * 86400, names=cfg.get("cache_dirs"), skip=recent,
                                leftovers_max_mb=cfg.get("worktree_leftovers_max_mb")):
            key = str(r["task"])
            if r["cleared"]:
                log(self.p, f"worktree {r['path']} of task {r['task']}: removed {', '.join(r['cleared'][:10])}")
            if r.get("moved"):
                mv = r["moved"]
                log(self.p, f"worktree {r['path']} of task {r['task']}: moved {mv['files']} untracked file(s) "
                            f"({mv['bytes'] / 1e6:.1f} MB) to {mv['to']}")
            if r.get("tracked"):
                dirty[key] = self._uncommitted(r, dirty.get(key))
            elif not r.get("held"):
                dirty.pop(key, None)
            if r["why"] is None:
                self._kept.pop(r["task"], None)
                logged.pop(key, None)
                log(self.p, f"worktree {r['path']} of task {r['task']} ({r['status']}) removed; "
                            f"branch {r['branch'] or '?'} kept")
            else:
                if logged.get(key) != r["why"]:   # once per reason, across restarts too
                    logged[key] = r["why"]
                    log(self.p, f"worktree {r['path']} of task {r['task']} kept: {r['why']}")
                # Held (see worktree.held_by): checked again every sweep (no git work), so it goes soon after the hold ends.
                self._kept[r["task"]] = (r["updated"], 0 if r.get("held") else now, r["why"])
        kept = {str(t): why for t, (_, _, why) in sorted(self._kept.items())
                if (self.p.worktrees / f"t{t}").exists()}
        if kept != (db.kv("worktrees_kept") or {}):
            db.set_kv("worktrees_kept", kept or None)
        for name, memo in ((KV_WORKTREES_LOGGED, logged), (KV_WORKTREES_DIRTY, dirty)):
            memo = {k: v for k, v in memo.items() if (self.p.worktrees / f"t{k}").exists()}
            if memo != (db.kv(name) or {}):
                db.set_kv(name, memo or None)

    def _uncommitted(self, r: dict, prev: dict | None) -> dict:
        """A finished task's worktree holds modified tracked files: raise it to the coordinator once
        per worktree and content (fingerprint); the worktree stays. Returns its KV_WORKTREES_DIRTY entry."""
        db, now = self.p.db, time.time()
        fp = f"{worktree.DIRTY_EVENT}:{r['task']}:{r['fingerprint']}"
        paths = r["tracked"]
        if not db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            task = db.task(r["task"]) or {}
            shown = ", ".join(paths[:5]) + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else "")
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", worktree.DIRTY_EVENT, fp, "normal",
                  f"#{r['task']} ({r['status']}) {(task.get('title') or '')[:80]} left uncommitted changes to tracked "
                  f"files in its worktree {r['path']}: {shown}. The worktree is kept; it is never removed while "
                  f"they are there. Decide: carry them on in a code task that continues #{r['task']} (commit them "
                  f"on its branch {r['branch'] or '?'}), or discard them if they are not needed.", "queued", r["task"]))
            log(self.p, f"worktree {r['path']} of task {r['task']}: uncommitted changes to {len(paths)} tracked "
                        f"file(s); raised to the coordinator")
        return {"status": r["status"], "paths": paths[:5], "count": len(paths), "fingerprint": r["fingerprint"],
                "since": (prev or {}).get("since") if (prev or {}).get("fingerprint") == r["fingerprint"] else now}

    def probe_waiting(self) -> None:
        """A waiting task may name a shell probe (`retry_when`) for the thing it waits on. The probe
        runs here, model-free and in the background. Exit 0 makes the task due at once. When its
        `retry_after_s` timer runs out while the probe still says "not yet", the task sleeps another
        `retry_after_s` instead of spending a worker run to find that out. "Not yet" is any exit in
        NOT_YET_RCS: 1, 75 (a busy `ttp lock`) and 255 (ssh could not reach the host a remote marker
        lives on). A broken probe
        (any other exit, a timeout, a probe that cannot start) wakes it at its timer so a worker can
        fix the probe, and `waiting.max_hold_s` after the hand-off it wakes whatever the probe says."""
        db, now = self.p.db, time.time()
        for tid, (proc, started, probe) in list(self._probes.items()):
            rc = proc.poll()
            if rc is None and now - started < PROBE_TIMEOUT_S:
                continue
            del self._probes[tid]
            if rc is None:
                _kill_group(proc)
            task = db.task(tid)
            if task and _current_probe(task) != probe:
                continue   # re-pointed (`set-when`) while it ran: its verdict is not the new probe's
            if task and task["status"] == "queued" and deferral(task).get("when"):
                self._start_verdict(task, "timeout" if rc is None else rc, now)
                continue
            self._probe_rc[tid] = ("timeout" if rc is None else rc, now, probe)
            if rc == 0:
                task = db.task(tid)
                if task and task["status"] == "queued" and (task["not_before"] or 0) > now:
                    self._wake_waiting(task, "probe passed", now)
        for t in db.q("SELECT * FROM tasks WHERE status='queued' AND not_before IS NOT NULL"):
            prev = load_result(t["result"])
            probe = prev.get("retry_when")
            if prev.get("status") != "waiting" or not isinstance(probe, str) or not probe.strip() \
                    or deferral(t).get("when"):
                continue
            self._drop_stale_probe(t["id"], probe)
            if t["not_before"] <= now:
                # Hand-offs from before the hold, and tasks already woken, keep their timer.
                if prev.get("woke") or not isinstance(prev.get("waiting_since"), (int, float)):
                    continue
                if not self._hold_waiting(t, prev, now):
                    continue
            elif t["id"] in self._probes or now - self._probed.get(t["id"], 0) < PROBE_EVERY_S:
                continue
            self._start_probe(t["id"], probe, now)
        self.probe_deferred(now)

    def probe_deferred(self, now: float) -> None:
        """A task added with `start_when` stays queued, undispatched, until its probe exits 0; the
        probe runs here like a waiting task's, once any `start_after` has passed. Exit 1 means not
        yet, and so do 75 (EX_TEMPFAIL, e.g. a busy `ttp lock`) and 255 (ssh could not reach the
        host: a reboot or a network blip). A broken
        probe (another exit, a timeout, a probe that cannot start) is raised to the
        coordinator once, never as a worker run, and keeps being tried. So is a deferral still not
        met after `coordinator.defer_max_days`."""
        db = self.p.db
        max_days = float(self.cfg["coordinator"].get("defer_max_days") or 14)
        for t in db.q("SELECT * FROM tasks WHERE status='queued' AND labels LIKE '%\"start_when:%'"):
            d = deferral(t)
            if not d.get("when") or (t["not_before"] or 0) > now:
                continue
            since = d.get("since") or t["created"]
            if now - since >= max_days * 86400:
                self._deferral_event(t, since, "deferral_expired",
                                     f"its start_when has not passed in {max_days:g} days: {d['when'][:300]}")
            self._drop_stale_probe(t["id"], d["when"])
            if t["id"] in self._probes or now - self._probed.get(t["id"], 0) < PROBE_EVERY_S:
                continue
            self._start_probe(t["id"], d["when"], now, "start_when")
            if t["id"] not in self._probes:
                self._start_verdict(t, self._probe_rc.pop(t["id"])[0], now)

    def _drop_stale_probe(self, tid: int, probe: str) -> None:
        """The task's probe was re-pointed (`ttp task set-when`): kill a run of the old one and forget
        its verdict, so it is never credited to the new probe."""
        run = self._probes.get(tid)
        if run and run[2] != probe:
            _kill_group(run[0])
            del self._probes[tid]
            self._probed.pop(tid, None)
        if tid in self._probe_rc and self._probe_rc[tid][2] != probe:
            del self._probe_rc[tid]
            self._probed.pop(tid, None)

    def _start_verdict(self, task: dict, rc, now: float) -> None:
        d = deferral(task)
        if rc == 0:
            self._probe_rc.pop(task["id"], None)
            self.p.db.update_task(task["id"], labels=without_deferral(json.loads(task["labels"] or "[]")),
                                  not_before=None)
            log(self.p, f"task {task['id']} start_when passed; ready to start")
        elif rc not in NOT_YET_RCS:
            why = rc if isinstance(rc, str) else f"exit {rc}"
            self._deferral_event(task, d.get("since") or task["created"], "deferral_probe_broken",
                                 f"its start_when probe is broken ({why}; only 0 = start and 1, 75 or 255 = not "
                                 f"yet are valid): {d.get('when', '')[:300]}")

    def _deferral_event(self, task: dict, since: float, kind: str, what: str) -> None:
        """Once per deferral: the coordinator decides again (fix the probe with task_update
        start_when, start it with start_when "", or cancel). The task stays deferred meanwhile."""
        db = self.p.db
        fp = f"{kind}:{task['id']}:{since:.0f}"
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            return
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (time.time(), "daemon", kind, fp, "normal",
              f"#{task['id']} {task['title']} waits to start, but {what}. The probe keeps running; decide: fix "
              f"it with task_update start_when, start the task now with start_when \"\", or cancel it.",
              "queued", task["id"]))
        log(self.p, f"task {task['id']} {kind}; raised to the coordinator")

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

    def _start_probe(self, tid: int, probe: str, now: float, what: str = "retry_when") -> None:
        self._probed[tid] = now
        try:
            proc = subprocess.Popen(probe, shell=True, cwd=str(self.p.root), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True, env=self._probe_env())
        except OSError as e:
            log(self.p, f"task {tid} {what} probe could not start: {e}")
            self._probe_rc[tid] = ("could not start", now, probe)
            return
        self._probes[tid] = (proc, now, probe)

    def _hold_waiting(self, task: dict, prev: dict, now: float) -> bool:
        """A waiting task whose timer ran out: wake it, or put it back to sleep while its probe says
        "not yet". True when a probe must run before that can be decided."""
        tid = task["id"]
        max_hold = float((self.cfg.get("waiting") or {}).get("max_hold_s") or 6 * 3600)
        since = float(prev["waiting_since"])
        rc, at, _ = self._probe_rc.get(tid, (None, 0.0, ""))
        fresh = at >= since and now - at <= 2 * PROBE_EVERY_S
        if fresh and rc == 0:
            self._wake_waiting(task, "probe passed", now)
        elif fresh and rc not in NOT_YET_RCS:
            self._wake_waiting(task, f"probe broken: {rc if isinstance(rc, str) else f'exit {rc}'}", now)
        elif now >= since + max_hold:
            self._wake_waiting(task, f"held {max_hold / 3600:g} h, probe still failing", now)
        elif fresh:
            # 75 is EX_TEMPFAIL (e.g. a busy `ttp lock`); 255 is ssh failing to reach the host
            # (a reboot or a network blip). Both mean "not yet".
            nb = min(now + _retry_s(prev), since + max_hold)
            what = str(prev.get("waiting_for") or prev.get("summary") or "")[:300]
            says = "host unreachable" if rc == 255 else "not yet"
            db = self.p.db
            db.update_task(tid, not_before=nb, blocked_reason=(
                f"waiting for {what}; its probe says {says}; next try "
                f"{time.strftime('%H:%M', time.localtime(nb))}")[:500])
            log(self.p, f"task {tid} retry_when probe still failing"
                        f"{' (host unreachable)' if rc == 255 else ''}; asleep until "
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

    def _probe_env(self) -> dict:
        """Probes run `ttp` (e.g. `ttp lock --probe device-a`) as a worker would."""
        runtime_dir = str(Path(__file__).resolve().parent.parent)
        return {**os.environ, "TTP_PROJECT": str(self.p.base), "PYTHONPATH": runtime_dir,
                "PATH": f"{self.p.harness / 'bin'}:{service_path()}:{os.environ.get('PATH', '')}"}

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
        its slot count (config `resources`, default 1; on a shared resource the smallest any project
        sharing it gives). A `resource:<name>` label means the task uses
        the resource for some commands: those take the resource's lock (`ttp lock`) or its own queue,
        so the rest of the task runs in parallel with other work instead of waiting for the slot.
        With reserve, a task kept out only by `ttp lock` commands reserves the resource so new ones
        wait; the reservation lapses unless the next dispatch refreshes it. The names in config
        `device.locks` are one resource with one slot.

        At most twice its slots run at once among the tasks that use a resource either way: more
        would only queue in `ttp lock` on a worker slot and a wall clock that other work could use."""
        paused = self.p.db.paused_resources().keys()
        if coord.task_resources(task) & paused:
            return False
        if locks.device_locks(self.cfg) and ({locks.canonical(self.cfg, r) for r in coord.task_resources(task)}
                                             & {locks.canonical(self.cfg, r) for r in paused}):
            return False   # a pause of one device name holds the tasks that name another
        running = self.p.db.q("SELECT labels FROM tasks WHERE status='running'")

        def _locks_of(names: list[str]) -> set[str]:
            return {locks.canonical(self.cfg, r) for r in names}

        for res in _shared(task):
            lock = locks.canonical(self.cfg, res)
            users = sum(1 for t in running if lock in _locks_of(_shared(t) + _exclusive(t)))
            if users >= 2 * shared.slots(self.p, lock, self.cfg):
                return False
        for res in _exclusive(task):
            lock = locks.canonical(self.cfg, res)
            limit = shared.slots(self.p, lock, self.cfg)
            # Running exclusive tasks count even before their supervisor has taken its slot; the
            # lock files show the slots `ttp lock` commands hold.
            busy = sum(1 for t in running if lock in _locks_of(_exclusive(t)))
            if busy >= limit:
                return False
            if not locks.any_free(self._slot_paths(res)):
                if reserve:
                    locks.reserve(self._reserve_path(res), shared.holder(self.p, res, f"task #{task['id']}", self.cfg))
                return False
        return True

    def _device_max_tasks(self) -> int:
        try:
            return max(int((self.cfg.get("device") or {}).get("max_tasks", 2) or 1), 1)
        except (TypeError, ValueError):
            return 2

    def _device_tasks_running(self) -> int:
        """Running tasks tagged needs_device. They prepare in parallel; `ttp lock` admits one at a
        time to the device phase, in arrival order."""
        return sum(1 for t in self.p.db.q("SELECT labels FROM tasks WHERE status='running'")
                   if needs_device(t, self.cfg))

    def _slot_paths(self, res: str) -> list[Path]:
        lock = locks.canonical(self.cfg, res)
        return locks.slot_paths(shared.locks_dir(self.p, lock, self.cfg), lock, shared.slots(self.p, lock, self.cfg))

    def _reserve_path(self, res: str) -> Path:
        lock = locks.canonical(self.cfg, res)
        return locks.reserve_path(shared.locks_dir(self.p, lock, self.cfg), lock)

    def _dispatchable(self) -> bool:
        """Whether any queued task could start now (dependencies done, resources free)."""
        dev_full = self._device_tasks_running() >= self._device_max_tasks()
        return any(self._resources_free(t) and not (dev_full and needs_device(t, self.cfg))
                   for t in self.p.db.ready_tasks())

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

    def _auto_review(self, task: dict, summary: str, add: bool = True) -> tuple[int, bool] | None:
        """The review of a finished code task, queued here the way the coordinator would, so a
        routine hand-off needs no coordinator turn: (review id, whether it was added now). An open
        review that already covers the task (queued ahead by the coordinator) is that review. None
        when delivery has no review step, `add` is off and none is open, the branch changes nothing,
        the review cap is reached or the diff cannot be read: the coordinator then decides."""
        cfg, db = self.cfg, self.p.db
        d = cfg.get("delivery") or {}
        rules = cfg.get("review") or {}
        if not rules.get("auto", True) or not (d.get("review_before_pr", True) or d.get("push_branch")):
            return None
        branch = task.get("branch")
        try:
            for t in db.q("SELECT * FROM tasks WHERE kind='review' AND status NOT IN ('done','failed','cancelled')"):
                if task["id"] in dependency_ids(t) or (branch and branch in worktree.reviewed_refs(self.p, t)):
                    return t["id"], False
            if not add:
                return None
            changes = worktree.diff_lines(self.p, [branch]) if branch else None
            full = worktree._git(self.p.root, "rev-parse", branch) if changes else ""
            head = full[:12]
        except Exception as e:
            log(self.p, f"task {task['id']}: no review queued, its diff was not read: {e}")
            return None
        if not changes or coord.next_task_slot(db, coord.task_cap(cfg, True), review=True) is not None:
            return None
        title = f"Review #{task['id']}: {task['title']}"[:200]
        dup = db.one("SELECT id FROM tasks WHERE title=? AND status NOT IN ('done','failed','cancelled')", (title,))
        if dup:
            return dup["id"], False
        # A fix after a failed review is re-reviewed as its continuation: the reviewer gets the earlier
        # findings, and the review is sized by what changed since the head they were found on.
        chain, c = {task["id"]}, continues_id(task)
        while c is not None and c not in chain:
            chain.add(c)
            c = continues_id(db.task(c) or {"labels": "[]"})
        prior = next((r for r in db.q("SELECT * FROM tasks WHERE kind='review' AND status='failed' ORDER BY id DESC")
                      if r["id"] in chain or chain & set(dependency_ids(r))), None)
        tier = bud.review_tier(changes, cfg)
        pr = task.get("pr_url")
        lines = [f"Independently review the change of code task #{task['id']} ({task['title']}): branch {branch}, "
                 f"head {head}" + (f", PR {pr}" if pr else "") + ".",
                 f"Its spec: {coord.clip(task.get('spec'), AUTO_REVIEW_SPEC_CHARS)}",
                 f"Its hand-off: {coord.clip(summary, AUTO_REVIEW_SUMMARY_CHARS)}"]
        if prior:
            lines.append(f"Review #{prior['id']} failed on an earlier head: check each of its findings is fixed, "
                         f"then review what changed since.")
        lines.append("Check that it does what its spec asks, is correct, keeps the charter's restrictions, and "
                     "that its tests fail without it.")
        # A head its PR already carries is delivered: a pass closes the review, with no push to the
        # push branch or approval for the push queue (that branch may be unrelated, or not exist yet).
        delivered = push.delivered_pr(self.p, task, full)
        pushes = bool(d.get("push_branch") and d.get("push_allowed", True)) and not delivered
        if delivered:
            lines.append(f"Its head is already delivered as PR {delivered}: review only. If it passes, hand off "
                         f"`done`; do not run `ttp push` or approve it for the push queue. Leave the branch and "
                         f"the PR as they are.")
        elif pushes:
            lines.append(f"If it passes, push it with `ttp push` from the change's worktree (it publishes to "
                         f"{d['push_branch']}).")
        else:
            lines.append("Review only: leave the branch" + (" and the PR" if pr else "") + " as they are.")
        if str(rules.get("auto_notes") or "").strip():
            lines.append(str(rules["auto_notes"]).strip())
        lines.append("Return the verdict and findings" + (", and the pushed commit." if pushes else "."))
        labels = [f"auto_review:{task['id']}"] + ([f"continues:{prior['id']}"] if prior else [])
        rid = db.add_task(title, "\n".join(lines), kind="review", tier=tier, priority=2, origin="daemon",
                          budget_usd=float(cfg["budget"]["task_default_usd"].get(tier, 8.0)),
                          depends_on=[task["id"]], labels=labels)
        log(self.p, f"task {task['id']}: queued review #{rid} ({tier})")
        return rid, True

    def _fix_failed_review(self, review: dict, fups: list[dict], summary: str) -> tuple[int, int, list[int]] | None:
        """A review that failed with fix specs, handled the way the coordinator would: one code task
        fixes them all on top of the reviewed branch, and a re-review that waits on it takes over the
        failed review's dependents: (fix id, re-review id, the dependents moved). None when auto
        reviews are off, the review covers no single code branch, AUTO_FIX_ROUNDS reviews of this
        stack already failed, a task cap is reached or a task already continues it: the coordinator
        then decides, and the dependents are blocked as before."""
        cfg, db = self.cfg, self.p.db
        d = cfg.get("delivery") or {}
        if not (cfg.get("review") or {}).get("auto", True) or not (d.get("review_before_pr", True) or d.get("push_branch")):
            return None
        # A re-review repeats the stack's first review's spec, not the nested specs of the rounds since.
        rounds, seen, c, first = 1, {review["id"]}, continues_id(review), review
        while c is not None and c not in seen and (t := db.task(c)):
            seen.add(c)
            if t["kind"] == "review":
                rounds, first = rounds + 1, t
            c = continues_id(t)
        if rounds > AUTO_FIX_ROUNDS or db.one("SELECT id FROM tasks WHERE labels LIKE ?",
                                              (f'%"continues:{review["id"]}"%',)):
            return None
        if coord.next_task_slot(db, coord.task_cap(cfg)) is not None \
                or coord.next_task_slot(db, coord.task_cap(cfg, True), review=True) is not None:
            return None
        spec = review.get("spec") or ""
        cands = [t for i in sorted(coord._covered(db, dependency_ids(review), spec))
                 for t in [db.task(i)] if t and t["kind"] == "code" and t["branch"]]
        try:
            # A review of a stack (a fix on top of its change) is fixed on the stack's tip.
            tips = [t for t in cands if all(o is t or worktree._closest_ancestor(self.p, [o["branch"]], t["branch"])
                                            for o in cands)]
            code = tips[0] if tips else None
            head = worktree._git(self.p.root, "rev-parse", "--short=12", code["branch"]) if code else ""
        except Exception as e:
            log(self.p, f"review {review['id']}: no fix queued, its branch was not read: {e}")
            return None
        if not code or not head:
            return None
        base_title = re.sub(r"^(?:Fix review #\d+: )+", "", code["title"])
        tier = code["tier"] if code["tier"] in bud.TIER_ORDER else "standard"
        found = "\n".join(f"{n}. {str(f['title'])[:200]}: {coord.clip(f.get('spec'), FOLLOWUP_SPEC_CHARS)}"
                          for n, f in enumerate(fups, 1))
        with db.tx():
            fid = db.add_task(f"Fix review #{review['id']}: {base_title}"[:200], "", kind="code", tier=tier,
                              priority=review["priority"], origin="daemon",
                              budget_usd=float(cfg["budget"]["task_default_usd"].get(tier, 8.0)),
                              labels=[f"continues:{code['id']}", f"review_fix:{review['id']}"])
            fbranch = f"ttp/t{fid}-{worktree.slug(base_title)}"
            rid = db.add_task(f"Re-review #{review['id']}: " + re.sub(r"^(?:Re-review #\d+: )+", "", review["title"]),
                              "\n".join([
                                  f"Re-review after review #{review['id']} failed. Fix task #{fid} (branch {fbranch}, "
                                  f"built on #{code['id']}'s branch {code['branch']} at {head}) addresses its findings:",
                                  found,
                                  f"Check each is fixed, then review what changed since. Where the first review's "
                                  f"spec below names a branch or worktree of this stack (such as {code['branch']} or "
                                  f"#{code['id']}'s worktree), use #{fid}'s branch and worktree.",
                                  f"The first review's spec (#{first['id']}):\n"
                                  f"{(first.get('spec') or '')[:REVIEW_FIX_SPEC_CHARS]}"]),
                              kind="review", tier=review["tier"], priority=review["priority"], origin="daemon",
                              budget_usd=float(cfg["budget"]["task_default_usd"].get(review["tier"], 8.0)),
                              depends_on=[fid], labels=[f"auto_review:{fid}", f"continues:{review['id']}"])
            db.update_task(fid, branch=fbranch, spec="\n".join([
                f"Fix the blocking findings of review #{review['id']} ({review['title']}) on code task "
                f"#{code['id']} ({code['title']}).",
                f"This task's branch starts from #{code['id']}'s branch {code['branch']} (head {head}): build on "
                f"it. Leave the push to re-review #{rid}, which checks each finding once this task is done.",
                f"Findings to fix:\n{found}",
                f"The review's hand-off: {coord.clip(summary, AUTO_REVIEW_SUMMARY_CHARS)}"]))
            moved = [t["id"] for t in coord._open_dependents(db, review["id"])]
            coord._take_over_dependents(db, review["id"], rid)
        log(self.p, f"review {review['id']} failed: queued fix #{fid} and re-review #{rid}"
                    + (f"; moved {moved} onto it" if moved else ""))
        return fid, rid, moved

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
        """Deduplicated broadcast, across daemon restarts too (an upgrade must not re-announce a
        condition the user already has). A high alert with a key opens an episode that clears
        itself once the condition does (alerts.sweep); while it is open a repeat only updates the
        episode's `last`, with one reminder after alerts.REMIND_S. Other alerts go out at most once
        per `every_s`; `every_s=0` is for callers that alert only when the condition starts again.
        A machine-wide condition is broadcast by the one project holding its claim; the others post
        it quietly (web app and status only)."""
        now = time.time()
        db = self.p.db
        tracked = alerts.tracked("alert", severity, key)
        claim_key = self._machine_wide(key) if tracked else None
        with db.tx():   # marked sent only together with the message
            sent = db.kv("alerts_sent", {})
            last = float(sent.get(key, 0))
            ep = db.one("SELECT * FROM alerts WHERE key=? AND cleared IS NULL ORDER BY id DESC LIMIT 1",
                        (key,)) if tracked else None
            if ep and every_s and (now - ep["raised"] < alerts.REMIND_S or last >= ep["raised"] + alerts.REMIND_S):
                db.x("UPDATE alerts SET last=? WHERE id=?", (now, ep["id"]))
                return
            if not ep and not tracked and now - last < every_s:
                return
            if claim_key:
                prev = db.one("SELECT MAX(cleared) AS c FROM alerts WHERE key=?", (key,))
                quiet = not alerts.claim(claim_key, key, str(self.p.base), now, float(prev["c"] or 0) if prev else 0.0)
            else:
                quiet = False
            sent[key] = now
            # Kept as long as the longest interval any alert uses, so a monthly one is not forgotten
            # (and re-sent) after a week.
            db.set_kv("alerts_sent", {k: v for k, v in sent.items() if now - float(v) < ALERT_KEEP_S})
            if ep and quiet:
                db.x("UPDATE alerts SET last=? WHERE id=?", (now, ep["id"]))   # the reminder is the claimer's
                return
            db.post("out", text, chat=None, channel=alerts.QUIET if quiet else "chat", kind="alert",
                    severity=severity, ref=key)

    def _machine_wide(self, key: str) -> str | None:
        """The per-user claim key of a condition every project on this machine sees alike: a
        logged-out CLI, or a low disk on the filesystem the guard found short. None otherwise."""
        if key.startswith("auth:"):
            return f"{hostname()}|{key}"
        if key == "disk":
            path = (self.p.db.kv("disk_low") or {}).get("path")
            try:
                return f"{hostname()}|disk|{os.stat(path).st_dev}" if path else None
            except OSError:
                return None
        return None

    def _cleared_claims(self, key: str) -> list[str]:
        """The claim keys a cleared machine-wide episode ends: the CLI's, or each filesystem this
        project's disk guard watches (the low one is no longer recorded once it cleared)."""
        if key.startswith("auth:"):
            return [f"{hostname()}|{key}"]
        out = []
        for path in (self.p.base, self.p.worktrees):
            try:
                out.append(f"{hostname()}|disk|{os.stat(path).st_dev}")
            except OSError:
                pass
        return out

    def sweep_alerts(self) -> None:
        """Close alert episodes whose condition cleared (stored with the time; the chats hear it once).
        Auth breakers go first: a logged-out alert lasts as long as its provider's breaker."""
        self.check_logins()
        for ep in alerts.sweep(self.p.db):
            log(self.p, f"alert cleared: {ep['key']} ({ep['cleared_why']})")
            if ep["key"].startswith("auth:") or ep["key"] == "disk":
                # Whoever holds the claim: the next project to see the condition again broadcasts it.
                alerts.release(self._cleared_claims(ep["key"]), ep["cleared"])
            kind, _, prov = ep["key"].partition(":")
            if kind == "auth":
                # Tasks dispatch skips for another reason (the disk guard) would keep a stale note.
                self.p.db.x("UPDATE tasks SET blocked_reason=NULL WHERE status='queued' AND blocked_reason LIKE ?",
                            (f"{LOGGED_OUT_NOTE} ({prov})%",))

    def slack(self):
        if not self.cfg["notify"].get("slack"):
            return None
        if self._slack is None:
            from .slack import from_config
            self._slack = from_config(self.cfg)
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
        # Only an ask that reached Slack can back a PR approval (prguard), so those go whatever their severity.
        approval_asks = {prguard.BLOCKING_REF + r for r in prguard.APPROVING_REASONS}
        for m in rows:
            wanted = SEVERITY_RANK.get(m["severity"], 1) >= floor or (m["kind"] == "ask" and m["ref"] in approval_asks)
            to_slack = ((m["chat"] is None and wanted and m["kind"] != "info"
                         and m["channel"] != alerts.QUIET and not cleared(db, m, time.time()))
                        or m["chat"] == "slack")
            if to_slack:
                try:
                    thread = m["ref"] if m["chat"] == "slack" else None
                    ts = sl.post(self.p.name, m["text"], thread_ts=thread)
                    with db.tx():   # the post's ts: pr_approve reads an ask back from Slack by it
                        db.x("UPDATE messages SET ext_id=? WHERE id=?", (ts, m["id"]))
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
                    db.post("in", text, chat="slack", channel="slack", kind="user", ref=m["ts"],
                            provenance="slack", ext_id=m["ts"])
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
                        db.post("in", text, chat="slack", channel="slack", kind="user", ref=parent,
                                provenance="slack", ext_id=r["ts"])
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


def needs_device(task: dict, cfg: dict) -> bool:
    """Tagged `needs_device`, or names a device lock (config `device.locks`) as a resource. Only with
    device locks configured: without them no task counts, and dispatch is as it always was."""
    dev = locks.device_locks(cfg)
    if not dev:
        return False
    labels = json.loads(task["labels"] or "[]")
    return "needs_device" in labels or any(lb.split(":", 1)[1] in dev for lb in labels
                                           if lb.startswith(("resource:", "exclusive:")))


def _detached_jobs(run_dir: Path) -> list[dict]:
    try:
        jobs = json.loads((run_dir / "detached.json").read_text())
    except (OSError, ValueError):
        return []
    return [j for j in jobs if isinstance(j, dict) and j.get("rc") and j.get("name")]


PAUSED_NOTE = "waits for a paused resource:"
LOGGED_OUT_NOTE = "held: logged out"
NET_HELD_NOTE = "held: network,"
LOGIN_CHECK_PASSED = "its login check passed"


def _names_commit(tasks: list[dict], head: str) -> bool:
    """Whether a task's spec names `head` by an abbreviation of at least 7 hex digits."""
    return any(head.startswith(h.lower()) for t in tasks for h in re.findall(r"\b[0-9a-fA-F]{7,40}\b", t.get("spec") or ""))


def idle_wake(p: Project, cfg: dict, gates: dict[str, dict], now: float, db=None) -> dict:
    """When the coordinator next wakes by itself, with no message or event pending. Both the daemon
    and `ttp status` / the web app read it from here, so the time shown is the one applied.

    The idle wake comes `idle_wake_s` after the last turn once no task is queued or running, and
    only while the core budget gate allows optional work. The idle-slot wake (`starve_state`) can
    come sooner. A wake turn that met the same state as the previous one had nothing new to
    decide: each such repeat doubles the wait for both, up to a day. Explicit check-backs use
    schedules.

    Returns `at` (next wake, possibly past), `due` ("idle", "starve" or None at `now`), `held`
    (why the idle wake cannot come), and the `wake` / `starve` kv values a starting turn stores."""
    # `db` is the caller's own connection: the web app calls this from its request threads, and an
    # SQLite connection works only in the thread that opened it.
    db, c = db or p.db, cfg["coordinator"]
    gate = gates.get(cfg.get("core_provider", "claude"))
    last = float(db.kv("last_coordinator_turn", 0))
    idle_s = float(c.get("idle_wake_s", 3600))
    fp = wake_fingerprint(p, gates, db)
    prev = db.kv("idle_wake", {}) or {}
    repeats = int(prev.get("n", 0)) if prev.get("fp") == fp else 0
    backoff = min(idle_s * 2 ** repeats, 86400.0) if repeats else 0.0
    idle_at, held = None, None
    if not db.one("SELECT id FROM tasks WHERE status IN ('queued','running')"):
        if gate is None or gate["allow_optional"]:
            idle_at = last + max(idle_s, backoff)
        else:
            held = f"held by the budget gate ({gate['level']})"
    starve = starve_state(db, cfg, gate, now)
    starve_at = last + max(backoff, starve["wait"]) if starve else None
    due = "idle" if idle_at is not None and now > idle_at else \
        "starve" if starve_at is not None and now > starve_at else None
    times = [t for t in (idle_at, starve_at) if t is not None]
    return {"at": min(times) if times else None, "due": due, "held": None if times else held,
            "wake": {"fp": fp, "n": repeats + 1}, "starve": starve}


def wake_fingerprint(p: Project, gates: dict[str, dict], db=None) -> str:
    """The state a wake turn decides on. Running counts as queued: dispatch moves tasks between
    the two without the coordinator. Spend numbers are left out; gate levels carry them."""
    db = db or p.db
    tasks = [(t["id"], "queued" if t["status"] == "running" else t["status"], t["priority"], t["depends_on"])
             for t in db.q("SELECT id, status, priority, depends_on FROM tasks "
                           "WHERE status NOT IN ('done','failed','cancelled') ORDER BY id")]
    asks = [r["id"] for r in db.q("SELECT id FROM messages WHERE kind='ask' AND handled=0 ORDER BY id")]
    scheds = [(s["name"], s["enabled"], s["every_s"], s["at"])
              for s in db.q("SELECT name, enabled, every_s, at FROM schedules ORDER BY name")]
    # Only red changes what a turn can do; green, yellow and orange are pacing and wake nobody.
    levels = sorted((k, g["level"] == "red", g["allow_new_work"]) for k, g in gates.items())
    files = [p.charter_path, p.config_path, p.memory_index,
             *(p.memory_dir.iterdir() if p.memory_dir.is_dir() else [])]
    mtimes = sorted((f.name, f.stat().st_mtime) for f in files if f.exists())
    paused = sorted(db.paused_resources())
    blob = json.dumps([tasks, asks, scheds, levels, mtimes] + ([paused] if paused else []), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def starve_state(db, cfg: dict, gate: dict | None, now: float) -> dict | None:
    """Paid plan capacity sitting idle: worker slots are free, the plan is below its line with room
    for all of them (green), and nothing is ready or about to be. Ask the coordinator for more
    independent work well before the idle wake would. Usage-billed work costs money whether or
    not it runs, so only plans qualify. A turn that adds no task doubles the wait for the next
    one, up to the idle wake; a new task resets it. While every queued task waits on its own probe
    or time, those gates wake the coordinator when they open: the wait starts at the idle wake and
    doubles up to a day. Returns None when this wake does not apply, else the wait after the last
    turn and the newest task id, stored as kv `starve` on firing."""
    c = cfg["coordinator"]
    if gate is None or gate["regime"] != "windows" or gate["level"] != "green" or not gate["allow_new_work"]:
        return None
    running = db.one("SELECT COUNT(*) n FROM runs WHERE provider=? AND status='running' AND role!='coordinator'",
                     (gate["provider"],))["n"]
    if running >= int(gate["max_parallel"]):
        return None
    # Queued work, whether ready or waiting on a retry timer, a dependency or a resource, starts by
    # itself; an open question waits for the user. A deferred task may sit for days: it holds nothing.
    held = [deferral(t) for t in db.q("SELECT labels FROM tasks WHERE status='queued'")]
    if any("when" not in d and d.get("after", 0) <= now for d in held) or \
            db.one("SELECT id FROM messages WHERE kind='ask' AND handled=0 AND ts>?", (now - OPEN_ASK_MAX_AGE_S,)):
        return None
    if coord.next_task_slot(db, coord.task_cap(cfg)) is not None:
        return None
    idle_s = float(c.get("idle_wake_s", 3600))
    base, cap = (idle_s, 86400.0) if held else (float(c.get("starve_wake_s", 300)), idle_s)
    newest = db.one("SELECT COALESCE(MAX(id),0) n FROM tasks")["n"]
    st = db.kv("starve") or {}
    wait = base if not st or newest > st.get("task", 0) else \
        min(max(float(st.get("wait", base)) * 2, base), max(cap, base))
    return {"task": newest, "wait": wait}


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
    elapsed -= float(exit_info.get("slept_s") or 0)   # a sleeping host spends nothing
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


def _proxied() -> bool:
    """Whether the agents reach their API through a proxy: the host then resolves nothing itself."""
    return any(os.environ.get(k) for k in PROXY_ENV)


def _background(fn) -> None:
    threading.Thread(target=fn, daemon=True).start()


def _resolves(host: str) -> bool:
    """Whether `host` resolves within REACH_TIMEOUT_S (getaddrinfo has no timeout of its own)."""
    ok: list[bool] = []

    def look() -> None:
        try:
            socket.getaddrinfo(host, 443)
            ok.append(True)
        except OSError:
            pass
    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(REACH_TIMEOUT_S)
    return bool(ok)


def _sleep_text(d: dict) -> str:
    n = int(d.get("sleeps") or 1)
    return (f"the host slept {float(d.get('slept_s') or 0) / 60:.0f} min"
            f"{f' over {n} sleeps' if n > 1 else ''}; {len(d.get('runs') or [])} running run(s) paused")


def _ids(ids: list) -> str:
    return ", ".join(str(i) for i in ids)


def _has_tokens(usage) -> bool:
    return bool(usage.input_tokens or usage.output_tokens or usage.cache_read_tokens or usage.cache_write_tokens)


def _resume_never_started(note: dict, usage) -> bool:
    """A run that was to continue a lost session ended before its agent did anything."""
    return bool(note.get("resumes")) and not usage.cost_usd and not usage.output_tokens


# `metrics.verdict` values that make a review handed off `done` a failed one (see review_rejects).
REVIEW_REJECT_VERDICTS = frozenset({"changes_needed", "changes_requested", "rejected"})


def review_rejects(result: dict) -> bool:
    """A review's hand-off says the change must not proceed through `metrics.verdict`, for projects
    whose kind-review.md reports `done` plus a verdict instead of `failed`."""
    metrics = result.get("metrics")
    verdict = metrics.get("verdict") if isinstance(metrics, dict) else None
    return isinstance(verdict, str) and verdict.strip().lower().replace("-", "_").replace(" ", "_") \
        in REVIEW_REJECT_VERDICTS


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


def _current_probe(task: dict) -> str | None:
    """The probe the daemon should be running for a task now: its start_when, else its waiting
    hand-off's retry_when."""
    when = deferral(task).get("when")
    if when:
        return when
    probe = load_result(task["result"]).get("retry_when")
    return probe if isinstance(probe, str) else None


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
    """pid runs: kill(pid, 0) finds it and it is not a zombie (ended, not reaped yet)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not zombie(pid)


def main() -> int:
    return Daemon(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()).run()


if __name__ == "__main__":
    sys.exit(main())
