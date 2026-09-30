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
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import budget as bud
from . import coordinator as coord
from . import locks
from . import runner
from . import schedule as sched
from . import screen as scr
from . import worktree
from .db import SEVERITY_RANK, TERMINAL_TASK_STATES, dump_result, load_result
from .project import Project, hostname, load_secrets
from .providers import get_provider
from .providers.base import last_json_object, service_path
from .providers.claude import as_windows
from .providers.jev import Jev, JevOutOfFunds

TICK_S = 3.0
LEASE_STALE_S = 180
HEARTBEAT_STALE_S = 300   # longer than any single tick step (a git fetch, a watcher command)
RESULT_FILE = "result.json"
PROBE_EVERY_S = 180     # how often a waiting task's `retry_when` probe runs
PROBE_TIMEOUT_S = 60


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
        self.gates: dict[str, bud.Gate] = {}
        self.cfg = self.p.config()
        self.jev = Jev(self.cfg, db=self.p.db)
        self._slack = None
        self._last_cfg = 0.0
        self._last_slack = 0.0
        self._reap_errors: dict[int, int] = {}
        self._start_failures = 0
        self._lock_fd: int | None = None
        self._started = time.time()
        self._healthy = False
        self._last_prune = 0.0
        self._disk_low: bool | None = None   # unknown until checked
        self._tick_errors = 0
        self._probes: dict[int, tuple[subprocess.Popen, float]] = {}
        self._probed: dict[int, float] = {}

    # lifecycle ------------------------------------------------------------------------------------
    def run(self) -> int:
        self.p.state.mkdir(parents=True, exist_ok=True)
        pidfile = self.p.state / "daemon.pid"
        if not self._single_instance():
            print(f"daemon already running (pid {_read_pid(pidfile)})", file=sys.stderr)
            return 1
        pidfile.write_text(str(os.getpid()))
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
                {"pid": os.getpid(), "host": hostname(), "started": self._started, "tick_errors": self._tick_errors}))
        except OSError:
            pass

    def _beat(self) -> None:
        """A completed tick. `status`, the web app and `ttp restart` read its age; the first one
        marks the harness commit this runtime is known to run on."""
        hb = self.p.state / "heartbeat"
        if not self._healthy:
            hb.write_text(json.dumps({"pid": os.getpid(), "host": hostname(), "started": self._started}))
            self._healthy = True
            try:
                head = subprocess.run(["git", "-C", str(self.p.harness), "rev-parse", "HEAD"], capture_output=True,
                                      text=True, timeout=30)
                if head.returncode == 0:
                    self.p.db.set_kv("harness_good", {"commit": head.stdout.strip(), "ts": time.time()})
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            os.utime(hb, None)

    def tick(self) -> None:
        now = time.time()
        if now - self._last_cfg > 10:
            self.cfg, self._last_cfg = self.p.config(), now
            self.jev = Jev(self.cfg, db=self.p.db)
        self.reap_runs()
        self.reconcile_tasks()
        self.prune_worktrees()
        if self.p.db.kv("paused", False):
            return
        self._refresh_meters()
        self.update_gates()
        coord.expire_asks(self.p, hold=any(g.level == "red" for g in self.gates.values()))
        self.run_schedules()
        self.poll_slack()
        self.maybe_coordinate()
        self.probe_waiting()
        self.dispatch()
        self.deliver_outbound()

    # runs -----------------------------------------------------------------------------------------
    def start_run(self, role: str, prompt: str, provider: str, tier: str, cwd: str, *, task: dict | None = None,
                  budget_usd: float | None = None, timeout_s: float | None = None, read_only: bool = False,
                  schema: dict | None = None, system: str | None = None, note: dict | None = None) -> int:
        tiers = self.cfg["providers"].get(provider, {}).get("tiers", {})
        model = tiers.get(tier, {}).get("model", "")
        prices = (self.cfg.get("pricing") or {}).get(provider) or {}
        prov = get_provider(provider).use(model, prices)
        effort = tiers.get(tier, {}).get("effort", "")
        restrictions = self.cfg.get("restrictions", {})
        argv, env = prov.build(role=role, model=model, effort=effort, cwd=cwd, budget_usd=budget_usd,
                               read_only=read_only, schema=schema, restrictions=restrictions)
        if not read_only:
            # Skill plugins this project enabled for its workers only (never the user's own setup).
            dirs = [str(Path(os.path.expanduser(d))) for d in
                    (self.cfg["providers"].get(provider, {}).get("plugin_dirs") or [])
                    if Path(os.path.expanduser(d)).is_dir()]
            roots = [str(self.p.state)] + [d for d in [worktree.git_common_dir(Path(cwd))] if d]
            extra = prov.writable_args(roots) + prov.plugin_args(dirs)
            # A trailing "-" (prompt on stdin) stays the last argument.
            argv = argv[:-1] + extra + ["-"] if argv[-1:] == ["-"] else argv + extra
        db = self.p.db
        run_id = db.x("INSERT INTO runs(task,role,provider,model,effort,account,started,boot_id,status,note) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (task["id"] if task else None, role, provider, model, effort, prov.account(), time.time(),
                       self.boot, "running", json.dumps(note or {})))
        run_dir = self.p.runs / str(run_id)
        # Raising from here on means nothing was launched: the run row must not stay "running".
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            if system is not None:
                (run_dir / "system.md").write_text(system)
                if provider == "claude":
                    argv = _with_system_prompt(provider, argv, run_dir / "system.md")
                else:   # no replaceable system prompt: the stable part leads the prompt instead
                    prompt = system + "\n\n" + prompt
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
                    "exclusive": [{"resource": res, "paths": [str(x) for x in self._slot_paths(res)],
                                   "reserve": str(locks.reserve_path(self.p.state / "locks", res))}
                                  for res in _exclusive(task)] if task else [],
                    "exclusive_wait_s": self.cfg["budget"].get("exclusive_wait_s", 600)}
            (run_dir / "run.json").write_text(json.dumps(spec, indent=1))
            with open(run_dir / "runner.log", "wb") as out:
                proc = subprocess.Popen([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=runtime_dir,
                                        env={**os.environ, "PYTHONPATH": runtime_dir}, stdout=out, stderr=out,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
        except BaseException:
            db.x("UPDATE runs SET status='failed', ended=? WHERE id=?", (time.time(), run_id))
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

    def reap_runs(self) -> None:
        for r in self.p.db.q("SELECT * FROM runs WHERE status='running'"):
            try:
                exit_file = self._run_dir(r) / "exit.json"
                if exit_file.exists():
                    self.finish_run(r, _read_result(exit_file) or {"rc": -1, "stopped": "lost", "ended": time.time()})
                elif not self._run_alive(r):
                    self.finish_run(r, {"rc": -1, "stopped": "lost", "ended": time.time()})
                self._reap_errors.pop(r["id"], None)
            except Exception:
                # One run whose end cannot be processed must not hold up the others or wedge the loop.
                n = self._reap_errors[r["id"]] = self._reap_errors.get(r["id"], 0) + 1
                log(self.p, f"run {r['id']} reap error {n}: " + traceback.format_exc().replace("\n", " | ")[:2000])
                if n >= 3:
                    self._abandon_run(r)

    def _abandon_run(self, r: dict) -> None:
        """Last resort for a run whose end keeps failing to process: close it so it cannot block the
        loop. Its task fails with the reason and the coordinator decides; a coordinator turn backs off."""
        db, now = self.p.db, time.time()
        why = f"run {r['id']} ended but its result could not be processed (details in the daemon log)"
        try:
            with db.tx():
                db.x("UPDATE runs SET status='failed', ended=? WHERE id=? AND status='running'", (now, r["id"]))
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
        for t, dep, why in db.dead_dependencies():
            reason = f"dependency #{dep} {why}" if dep is not None else "a dependency is not a task id"
            with db.tx():
                db.update_task(t["id"], status="blocked", blocked_reason=reason)
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (now, "daemon", "task_blocked", "normal", f"#{t['id']} {t['title']} is blocked: {reason}. "
                      f"The coordinator can re-point it with task_update depends_on (an empty list clears it), "
                      f"requeue it once the dependency is redone, or cancel it.", "handled", t["id"]))

    def finish_run(self, r: dict, exit_info: dict) -> None:
        db, p = self.p.db, self.p
        run_dir = self._run_dir(r)
        prov = get_provider(r["provider"]).use(r["model"] or "", (self.cfg.get("pricing") or {}).get(r["provider"]))
        usage = prov.parse(run_dir / "output.jsonl", run_dir / "stderr.log")
        if usage.estimated and not usage.cost_usd:
            usage.cost_usd = bud.estimate_cost(db, self.cfg, r["provider"], r["model"] or "", {
                "input": usage.input_tokens, "output": usage.output_tokens,
                "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens})
        stopped = exit_info.get("stopped")
        if usage.estimated and not usage.cost_usd and stopped:
            usage.cost_usd = _cut_off_cost(run_dir, exit_info)
        status = "ok" if exit_info.get("rc") == 0 and not usage.error else "failed"
        if stopped in ("timeout", "budget", "stopped", "lost", "stalled", "shutdown", "resource_busy"):
            status = stopped if stopped != "stopped" else "killed"
        if usage.limited:
            status = "limit"
        if usage.auth_failed:
            status = "auth"
        source = self._source_for(r)
        # The run's end, its spend and what it did to its task commit together: a daemon stopped
        # half way leaves the run "running", and the next tick processes it again from disk.
        with db.tx():
            db.x("UPDATE runs SET ended=?, status=?, exit_code=?, cost_usd=?, cost_estimated=?, input_tokens=?, "
                 "output_tokens=?, cache_read_tokens=?, cache_write_tokens=? WHERE id=?",
                 (exit_info.get("ended", time.time()), status, exit_info.get("rc"), usage.cost_usd,
                  int(usage.estimated), usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                  usage.cache_write_tokens, r["id"]))
            db.spend(r["provider"], usage.cost_usd, source, account=r["account"] or "", estimated=usage.estimated,
                     tokens_in=usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
                     tokens_out=usage.output_tokens)
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
                db.set_kv(f"limited:{r['provider']}", {"until": time.time() + 900, "note": "logged out"})
                self.alert(f"auth:{r['provider']}",
                           f"{r['provider']} on {hostname()} is logged out ({(usage.final_text or usage.error)[:120]}). "
                           f"Log in once on that machine ({prov.login_hint}). "
                           f"Work resumes by itself; queued messages are kept.", "high", every_s=4 * 3600)
            note = json.loads(r["note"] or "{}")
            if r["role"] == "coordinator":
                self._finish_coordinator(r, usage, status, note)
            else:
                self._finish_worker(r, usage, status, run_dir)
        log(p, f"run {r['id']} end status={status} cost=${usage.cost_usd:.3f}"
               f"{' (estimated)' if usage.estimated else ''} role={r['role']}")

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
        if (status == "lost" and not r["dir"]) or status == "shutdown":
            return   # never launched, or ended by `ttp stop --kill`: its messages and events stay queued
        if status == "auth":
            db.set_kv("coordinator_backoff_until", time.time() + 900)
            return
        if status != "ok" or not isinstance(actions, list):
            self._coordinator_failed(f"{status} {usage.error[:200]}")
            return
        db.set_kv("coordinator_failures", 0)
        default_chat = note.get("default_chat")
        problems = coord.apply(self.p, actions, default_chat=default_chat, user_turn=bool(note.get("messages")))
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

    def _finish_worker(self, r: dict, usage, status: str, run_dir: Path) -> None:
        db = self.p.db
        task = db.task(r["task"]) if r["task"] else None
        if not task:
            return
        result = _read_result(run_dir / RESULT_FILE) or last_json_object(usage.final_text or "") or {}
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
        if status == "shutdown":
            if isinstance(result, dict) and result.get("status"):
                status = "ok"   # it handed off before the stop reached it
            else:
                # The project was stopped, not the task: it resumes on the next start, on its own branch.
                db.update_task(task["id"], status="queued", blocked_reason="interrupted by `ttp stop --kill`; resumes")
                return
        if status == "resource_busy":
            # The run lost the race for its resource and never started its agent: not an attempt.
            db.update_task(task["id"], status="queued", not_before=time.time() + 30,
                           blocked_reason="its resource stayed busy before the run could start; retries")
            return
        rstatus = result.get("status") if isinstance(result, dict) else None
        summary = str((result.get("summary") if isinstance(result, dict) else None) or (usage.final_text or "")[:1500])
        waiting = status == "ok" and rstatus == "waiting"
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
        elif status in ("limit", "auth"):
            new = "queued"   # not an attempt: the account refused, the task did not fail
        else:
            new = "failed"
        attempts = int(task["attempts"] or 0) + (0 if status in ("limit", "auth") or waiting else 1)
        if new == "failed" and attempts < int(task["max_attempts"] or 3) and status in ("failed", "lost", "timeout",
                                                                                     "stalled", "no_handoff"):
            new = "queued"
        extra: dict = {}
        reason = None
        not_before = None
        if waiting:
            try:
                waits = int(load_result(task["result"]).get("waits") or 0) + 1
            except (TypeError, ValueError):
                waits = 1
            what = str(result.get("waiting_for") or summary)[:300]
            extra["waits"] = waits
            if waits > int(self.cfg["budget"].get("max_waits", 24)):
                new, reason = "blocked", f"still waiting after {waits} tries: {what}"
            else:
                try:
                    retry = min(max(float(result.get("retry_after_s") or 1800), 300.0), 6 * 3600.0)
                except (TypeError, ValueError):
                    retry = 1800.0
                not_before = time.time() + retry
                reason = f"waiting for {what}; next try {time.strftime('%H:%M', time.localtime(not_before))}"
        upd = {"status": new, "attempts": attempts, "result": dump_result(
            {"summary": summary, "status": rstatus or status, **extra,
             **({k: v for k, v in result.items() if k not in ("summary", "waits")}
                if isinstance(result, dict) else {})})}
        if reason:
            upd["blocked_reason"] = reason[:500]
        elif new == "blocked":
            upd["blocked_reason"] = str(result.get("question") or result.get("blocked_reason") or summary)[:500]
        if not_before:
            upd["not_before"] = not_before
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
            chat = None if task["reply_chat"] == "all" else task["reply_chat"]
            db.post("out", text[:6000], chat=chat, kind="reply", severity="normal")
        sev = "high" if new == "blocked" else "normal"
        fups = result.get("followups") if isinstance(result, dict) else None
        fups = [f for f in fups if isinstance(f, dict) and f.get("title")] if isinstance(fups, list) else []
        text = (f"#{task['id']} {task['title']} → {new} (run {status}, {'~' if usage.estimated else ''}"
                f"${usage.cost_usd:.2f}): {summary[:1200]}")
        if isinstance(result, dict):
            # A plan's findings and plugin advice reach the coordinator, which decides what to keep.
            facts = [f for f in (result.get("findings") or []) if isinstance(f, dict) and f.get("fact")][:12]
            if facts:
                text += "\nFindings (save the durable ones as memory):" + "".join(
                    f"\n- {str(f['fact'])[:240]} [{str(f.get('source', ''))[:120]}]" for f in facts)
            plugs = [x for x in (result.get("enable_plugins") or []) if isinstance(x, dict) and x.get("path")][:6]
            if plugs:
                text += "\nRecommended skill plugins for workers:" + "".join(
                    f"\n- {str(x['path'])[:200]}: {str(x.get('why', ''))[:160]}" for x in plugs)
        if len(fups) > 5:
            text += " | more proposed follow-ups: " + "; ".join(str(f["title"])[:120] for f in fups[5:])[:1500]
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), f"task:{task['id']}", f"task_{new}", sev, text, "queued", task["id"]))
        for f in fups[:5]:
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "followup_proposed", "normal",
                  f"proposed follow-up: {f['title']} — {str(f.get('spec', ''))[:600]}", "queued", task["id"]))

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

    def update_gates(self) -> None:
        windows = bud.plan_windows(self.p.db)
        gates = {}
        for prov in {self.cfg.get("core_provider", "claude"), *[t["provider"] for t in self.p.db.q(
                "SELECT DISTINCT provider FROM tasks WHERE provider IS NOT NULL AND status IN ('queued','running')")]}:
            g = bud.evaluate(self.p.db, self.cfg, prov, windows)
            lim = self.p.db.kv(f"limited:{prov}")
            if lim and lim.get("until", 0) > time.time():
                bud._raise(g, "red", f"provider limit: {lim.get('note')}")
                g.max_parallel, g.allow_new_work, g.allow_optional = 0, False, False
            prev = self.gates.get(prov)
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
                self.p.db.post("out", f"Budget for {prov} is now {g.level}: {'; '.join(g.reasons) or 'back to normal'}. "
                               + hint, chat=None, kind="alert", severity=sev)
            gates[prov] = g
        self.gates = gates
        self.p.db.set_kv("gates", {k: v.as_dict() for k, v in gates.items()})

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

    def _run_command_watcher(self, s: dict, payload: dict) -> str:
        cmd = payload.get("command")
        if not cmd:
            return "no command"
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=str(self.p.root),
                                 timeout=int(payload.get("timeout_s", 120)), env={**os.environ, "PATH": service_path()})
        except subprocess.TimeoutExpired:
            self.observe(f"watcher:{s['name']}", f"watcher command timed out: {cmd}", "normal")
            return "timeout"
        text = (out.stdout or "").strip()
        if out.returncode not in (0, 1) and not text:
            text = f"watcher command failed rc={out.returncode}: {(out.stderr or '')[-500:]}"
        n = 0
        for obs in _observations(text):
            self.observe(f"watcher:{s['name']}", obs.get("text", ""), obs.get("severity"))
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
        spec = payload.get("spec") or s["description"]
        prompt_file = payload.get("prompt")
        if prompt_file and (self.p.harness / "prompts" / prompt_file).exists():
            spec = (self.p.harness / "prompts" / prompt_file).read_text() + "\n\n" + spec
        db.add_task(f"[{s['name']}] {s['description'][:120] or 'recurring task'}", spec, kind=payload.get("kind", "work"),
                    tier=payload.get("tier", "standard"), priority=int(payload.get("priority", 4)),
                    budget_usd=s["budget_usd_day"], origin="schedule", labels=[s["name"]])
        return "queued"

    def observe(self, source: str, text: str, hint: str | None = None) -> None:
        if not text.strip():
            return
        try:
            v = scr.screen(self.p.db, self.cfg, source, text, hint, jev=self.jev)
        except JevOutOfFunds:
            self.alert("jev-funds", "The Jev account is out of credits. Screening falls back to rules "
                       "(more model calls, same coverage). Top up the Jev account to restore the savings.", "high")
            v = scr.screen(self.p.db, self.cfg, source, text, hint, jev=None)
        if v.wake:
            self.p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                        (time.time(), source, "observation", v.fingerprint, v.severity, text[:4000], "queued"))

    # coordinator ------------------------------------------------------------------------------------
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
        lim = db.kv(f"limited:{self.cfg.get('core_provider', 'claude')}")
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
        blob = json.dumps([tasks, asks, scheds, gates, mtimes], default=str)
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
        made = db.one("SELECT COUNT(*) n FROM tasks WHERE origin='coordinator' AND created>?",
                      (time.time() - 86400,))["n"]
        if made >= int(c.get("max_new_tasks_per_day", 40)):
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
        if ready and not self._disk_ok():
            return
        committed = None    # what running work under the dollar caps may still spend
        for task in ready:
            provider = task["provider"] or self.cfg.get("core_provider", "claude")
            gate = self.gates.get(provider) or bud.evaluate(db, self.cfg, provider, bud.plan_windows(db))
            if not gate.allow_new_work or busy.get(provider, 0) >= gate.max_parallel:
                continue
            if task["origin"] in ("schedule", "harness") and not gate.allow_optional:
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
            if not self._resources_free(task, reserve=True):
                continue
            tier = bud.clamp_tier(task["tier"], gate)
            try:
                cwd, branch = self._workdir_for(task)
            except Exception as e:
                db.update_task(task["id"], status="blocked", blocked_reason=f"workspace: {e}"[:400])
                continue
            from .prompts import worker_prompt
            try:
                prompt = worker_prompt(self.p, task, cwd, branch)
                db.update_task(task["id"], status="running", branch=branch, blocked_reason=None)
                self.start_run("worker" if task["kind"] != "review" else "reviewer", prompt, provider, tier, cwd,
                               task=task, budget_usd=max(remaining, 0.5) if task["budget_usd"] else None,
                               read_only=False)
            except Exception as e:
                self._start_failed(task, e)
                continue
            self._start_failures = 0
            busy[provider] = busy.get(provider, 0) + 1
            if gate.regime == "caps":
                committed += cost

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

    def _disk_ok(self) -> bool:
        """A full disk corrupts state and fails runs half way, so below `disk.min_free_gb` under the
        project folder no new worker starts. Running work, coordinator turns and replies continue."""
        need = float(self.cfg.get("disk", {}).get("min_free_gb", 2)) * 1e9
        low = None
        for path in {self.p.base.resolve(), self.p.worktrees.resolve()} if need > 0 else ():
            try:
                free = shutil.disk_usage(path).free
            except OSError:
                continue
            if free < need:
                low = (path, free)
        if bool(low) != self._disk_low:
            if low or self._disk_low:
                log(self.p, f"disk low: {low[1] / 1e9:.1f} GB free under {low[0]}; no new worker runs" if low
                    else "disk space ok again")
            self.p.db.set_kv("disk_low", {"path": str(low[0]), "free_gb": round(low[1] / 1e9, 1)} if low else None)
        self._disk_low = bool(low)
        if low:
            self.alert("disk", f"Only {low[1] / 1e9:.1f} GB free under {low[0]} (minimum {need / 1e9:g} GB). No new "
                       f"worker runs start until space is freed; running work and replies continue. Finished "
                       f"tasks' worktrees are removed after `disk.worktree_retention_days` once pushed.", "high",
                       every_s=24 * 3600)
        return not low

    def prune_worktrees(self, every_s: float = 3600) -> None:
        """Remove worktrees of tasks finished more than `disk.worktree_retention_days` ago, only when
        removing loses nothing (see worktree.keep_reason). Branches are never deleted."""
        now = time.time()
        if now - self._last_prune < every_s or not self.p.worktrees.is_dir():
            return
        self._last_prune = now
        days = float(self.cfg.get("disk", {}).get("worktree_retention_days", 7))
        if days <= 0:
            return
        for path in sorted(self.p.worktrees.iterdir()):
            m = re.fullmatch(r"t(\d+)", path.name)
            task = self.p.db.task(int(m.group(1))) if m else None
            if not task or task["status"] not in TERMINAL_TASK_STATES or now - float(task["updated"] or now) < days * 86400:
                continue
            try:
                why = worktree.keep_reason(self.p, path)
                if why is None:
                    worktree.remove(self.p, task["id"])
                    log(self.p, f"worktree {path} of task {task['id']} ({task['status']}) removed; "
                                f"branch {task['branch'] or '?'} kept")
            except Exception as e:
                log(self.p, f"worktree {path} not removed: {e}")

    def probe_waiting(self) -> None:
        """A waiting task may name a shell probe (`retry_when`) for the thing it waits on. The probe
        runs here, model-free and in the background; when it exits 0 the task is due at once, so no
        worker run is spent finding out that the wait is not over. `retry_after_s` stays the fallback."""
        db, now = self.p.db, time.time()
        for tid, (proc, started) in list(self._probes.items()):
            rc = proc.poll()
            if rc is None and now - started < PROBE_TIMEOUT_S:
                continue
            del self._probes[tid]
            if rc is None:
                _kill_group(proc)
            elif rc == 0:
                task = db.task(tid)
                if task and task["status"] == "queued" and (task["not_before"] or 0) > now:
                    db.update_task(tid, not_before=now)
                    log(self.p, f"task {tid} retry_when probe passed; dispatching")
        for t in db.q("SELECT id, result FROM tasks WHERE status='queued' AND not_before>?", (now,)):
            prev = load_result(t["result"])
            probe = prev.get("retry_when")
            if (prev.get("status") != "waiting" or not isinstance(probe, str) or not probe.strip()
                    or t["id"] in self._probes or now - self._probed.get(t["id"], 0) < PROBE_EVERY_S):
                continue
            self._probed[t["id"]] = now
            try:
                proc = subprocess.Popen(probe, shell=True, cwd=str(self.p.root), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        start_new_session=True)
            except OSError as e:
                log(self.p, f"task {t['id']} retry_when probe could not start: {e}")
                continue
            self._probes[t["id"]] = (proc, now)

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
        wait; the reservation lapses unless the next dispatch refreshes it."""
        limits = self.cfg.get("resources", {})
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
        daemon restarts too (an upgrade must not re-announce a condition the user already has)."""
        now = time.time()
        sent = self.p.db.kv("alerts_sent", {})
        if now - float(sent.get(key, 0)) < every_s:
            return
        sent[key] = now
        self.p.db.set_kv("alerts_sent", {k: v for k, v in sent.items() if now - float(v) < 7 * 86400})
        self.p.db.post("out", text, chat=None, kind="alert", severity=severity)

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
        db = self.p.db
        floor = SEVERITY_RANK.get(self.cfg["notify"].get("slack_min_severity", "high"), 2)
        last = int(db.kv("slack_last_out", 0))
        rows = db.q("SELECT * FROM messages WHERE direction='out' AND id>? ORDER BY id LIMIT 20", (last,))
        for m in rows:
            to_slack = (m["chat"] is None and SEVERITY_RANK.get(m["severity"], 1) >= floor) or m["chat"] == "slack"
            if to_slack:
                try:
                    thread = m["ref"] if m["chat"] == "slack" else None
                    ts = sl.post(self.p.name, m["text"], thread_ts=thread)
                    threads = set(db.kv("slack_threads", []))
                    threads.add(ts)
                    db.set_kv("slack_threads", sorted(threads)[-500:])
                except Exception as e:
                    log(self.p, f"slack post failed: {e}")
                    return
            db.set_kv("slack_last_out", m["id"])

    def poll_slack(self) -> None:
        sl = self.slack()
        now = time.time()
        if not sl or now - self._last_slack < float(self.cfg["notify"].get("slack_poll_s", 20)):
            return
        self._last_slack = now
        from .slack import projects_in_dm, route
        db = self.p.db
        oldest = str(db.kv("slack_oldest", f"{now - 60:.6f}"))
        try:
            msgs = sl.poll(oldest)
            siblings = projects_in_dm(sl.call("conversations.history", channel=sl.dm_channel(), limit=200)
                                      .get("messages", [])) or [self.p.name]
        except Exception as e:
            log(self.p, f"slack poll failed: {e}")
            return
        threads = set(db.kv("slack_threads", []))
        for m in msgs:
            text = route(m, self.p.name, threads, sorted(set(siblings) | {self.p.name}))
            if text:
                db.post("in", text, chat="slack", channel="slack", kind="user", ref=m.get("thread_ts") or m["ts"])
            elif not m.get("thread_ts") and sorted(set(siblings) | {self.p.name})[0] == self.p.name \
                    and not re.match(r"^\s*[A-Za-z0-9._-]+\s*:", m.get("text") or ""):
                names = ", ".join(sorted(set(siblings) | {self.p.name}))
                sl.post(self.p.name, f"Which project is this for? Start the message with one of: {names}, "
                                     f"e.g. `{self.p.name}: ...`", thread_ts=m["ts"])
            db.set_kv("slack_oldest", m["ts"])

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


def _exclusive(task: dict) -> list[str]:
    return [lb.split(":", 1)[1] for lb in json.loads(task["labels"] or "[]") if lb.startswith("exclusive:")]


def _cut_off_cost(run_dir: Path, exit_info: dict) -> float:
    """A run stopped before its provider reported any usage (Codex and Cursor report it only at the
    end) still spent money. Book the elapsed share of its dollar budget rather than $0, so the
    caps keep counting it."""
    try:
        spec = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        return 0.0
    budget, timeout = float(spec.get("budget_usd") or 0), float(spec.get("timeout_s") or 0)
    elapsed = float(exit_info.get("ended") or time.time()) - float(exit_info.get("started") or 0)
    if budget <= 0 or timeout <= 0 or not exit_info.get("started"):
        return 0.0
    return round(budget * min(max(elapsed, 0.0) / timeout, 1.0), 4)


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


def _read_result(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


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
