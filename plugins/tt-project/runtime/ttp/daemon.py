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

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import budget as bud
from . import coordinator as coord
from . import runner
from . import schedule as sched
from . import screen as scr
from . import worktree
from .db import SEVERITY_RANK
from .project import Project, hostname, load_secrets
from .providers import get_provider
from .providers.base import last_json_object, service_path
from .providers.claude import as_windows
from .providers.jev import Jev, JevOutOfFunds

TICK_S = 3.0
LEASE_STALE_S = 180
RESULT_FILE = "result.json"


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

    # lifecycle ------------------------------------------------------------------------------------
    def run(self) -> int:
        self.p.state.mkdir(parents=True, exist_ok=True)
        pidfile = self.p.state / "daemon.pid"
        other = _read_pid(pidfile)
        if other and other != os.getpid() and _alive(other):
            print(f"daemon already running (pid {other})", file=sys.stderr)
            return 1
        pidfile.write_text(str(os.getpid()))
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
            except Exception:  # a bad tick must never kill the daemon
                log(self.p, "tick error: " + traceback.format_exc().replace("\n", " | ")[:2000])
                time.sleep(10)
            time.sleep(TICK_S)
        log(self.p, "daemon stop")
        if _read_pid(pidfile) == os.getpid():
            pidfile.unlink(missing_ok=True)
        return 0

    def tick(self) -> None:
        now = time.time()
        if now - self._last_cfg > 10:
            self.cfg, self._last_cfg = self.p.config(), now
            self.jev = Jev(self.cfg, db=self.p.db)
        if self.p.db.kv("paused", False):
            self.reap_runs()
            return
        self.reap_runs()
        self._refresh_meters()
        self.update_gates()
        self.run_schedules()
        self.poll_slack()
        self.maybe_coordinate()
        self.dispatch()
        self.deliver_outbound()

    # runs -----------------------------------------------------------------------------------------
    def start_run(self, role: str, prompt: str, provider: str, tier: str, cwd: str, *, task: dict | None = None,
                  budget_usd: float | None = None, timeout_s: float | None = None, read_only: bool = False,
                  schema: dict | None = None, system: str | None = None, note: dict | None = None) -> int:
        prov = get_provider(provider)
        tiers = self.cfg["providers"].get(provider, {}).get("tiers", {})
        model = tiers.get(tier, {}).get("model", "")
        effort = tiers.get(tier, {}).get("effort", "")
        restrictions = self.cfg.get("restrictions", {})
        argv, env = prov.build(role=role, model=model, effort=effort, cwd=cwd, budget_usd=budget_usd,
                               read_only=read_only, schema=schema, restrictions=restrictions)
        db = self.p.db
        run_id = db.x("INSERT INTO runs(task,role,provider,model,effort,account,started,boot_id,status,note) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (task["id"] if task else None, role, provider, model, effort, prov.account(), time.time(),
                       self.boot, "running", json.dumps(note or {})))
        run_dir = self.p.runs / str(run_id)
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
                "budget_usd": budget_usd if provider not in ("claude",) else None}
        (run_dir / "run.json").write_text(json.dumps(spec, indent=1))
        with open(run_dir / "runner.log", "wb") as out:
            proc = subprocess.Popen([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=runtime_dir,
                                    env={**os.environ, "PYTHONPATH": runtime_dir}, stdout=out, stderr=out,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        db.x("UPDATE runs SET pid=?, dir=? WHERE id=?", (proc.pid, str(run_dir), run_id))
        log(self.p, f"run {run_id} start role={role} provider={provider} tier={tier} task={task and task['id']}")
        return run_id

    def reap_runs(self) -> None:
        db = self.p.db
        for r in db.q("SELECT * FROM runs WHERE status='running'"):
            run_dir = Path(r["dir"] or self.p.runs / str(r["id"]))
            exit_file = run_dir / "exit.json"
            if exit_file.exists():
                self.finish_run(r, json.loads(exit_file.read_text()))
                continue
            lease = run_dir / "lease"
            stale = (not lease.exists()) or time.time() - lease.stat().st_mtime > LEASE_STALE_S
            gone = r["boot_id"] != self.boot or not (r["pid"] and _alive(r["pid"]))
            if stale and gone:
                self.finish_run(r, {"rc": -1, "stopped": "lost", "ended": time.time()})

    def finish_run(self, r: dict, exit_info: dict) -> None:
        db, p = self.p.db, self.p
        run_dir = Path(r["dir"])
        prov = get_provider(r["provider"])
        usage = prov.parse(run_dir / "output.jsonl", run_dir / "stderr.log")
        stopped = exit_info.get("stopped")
        status = "ok" if exit_info.get("rc") == 0 and not usage.error else "failed"
        if stopped in ("timeout", "budget", "stopped", "lost", "stalled"):
            status = stopped if stopped != "stopped" else "killed"
        if usage.limited:
            status = "limit"
        if usage.auth_failed:
            status = "auth"
        source = self._source_for(r)
        db.x("UPDATE runs SET ended=?, status=?, exit_code=?, cost_usd=?, cost_estimated=?, input_tokens=?, "
             "output_tokens=?, cache_read_tokens=?, cache_write_tokens=? WHERE id=?",
             (exit_info.get("ended", time.time()), status, exit_info.get("rc"), usage.cost_usd, int(usage.estimated),
              usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens, r["id"]))
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
                       f"{r['provider']} refused work: {usage.limit_note}. Heavy work on it is paused for an hour; "
                       f"the account ({r['account'] or 'unknown'}) may need more credits or a higher cap.", "high")
        if usage.auth_failed:
            # Logged out is not a task failure and not worth retrying blindly: pause this provider,
            # say exactly how to fix it, and probe again every 15 minutes (a cheap decision turn).
            db.set_kv(f"limited:{r['provider']}", {"until": time.time() + 900, "note": "logged out"})
            self.alert(f"auth:{r['provider']}",
                       f"{r['provider']} on {hostname()} is logged out ({(usage.final_text or usage.error)[:120]}). "
                       f"Log in once on that machine (for Claude Code: run `claude` there and use /login). "
                       f"Work resumes by itself; queued messages are kept.", "high", every_s=4 * 3600)
        log(p, f"run {r['id']} end status={status} cost=${usage.cost_usd:.3f} role={r['role']}")
        note = json.loads(r["note"] or "{}")
        if r["role"] == "coordinator":
            self._finish_coordinator(r, usage, status, note)
        else:
            self._finish_worker(r, usage, status, run_dir)

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
        if status == "auth":
            db.set_kv("coordinator_backoff_until", time.time() + 900)
            return
        if status != "ok" or not isinstance(actions, list):
            fails = int(db.kv("coordinator_failures", 0)) + 1
            db.set_kv("coordinator_failures", fails)
            db.set_kv("coordinator_backoff_until", time.time() + min(1800, 30 * 2 ** fails))
            if fails >= 3:
                self.alert("coordinator", f"The coordinator failed {fails} turns in a row (last: {status} "
                           f"{usage.error[:200]}). Messages are queued, not lost.", "high")
            return
        db.set_kv("coordinator_failures", 0)
        default_chat = note.get("default_chat")
        problems = coord.apply(self.p, actions, default_chat=default_chat)
        ids = note.get("messages", [])
        if ids:
            db.x(f"UPDATE messages SET handled=1 WHERE id IN ({','.join('?' * len(ids))})", ids)
        evs = note.get("events", [])
        if evs:
            db.x(f"UPDATE events SET status='handled' WHERE id IN ({','.join('?' * len(evs))})", evs)
        if problems:
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (time.time(), "daemon", "rejected_actions", "normal", "; ".join(problems)[:1500], "queued"))
        db.set_kv("last_coordinator_summary", {"ts": time.time(), "summary": (out or {}).get("summary", "")})

    def _finish_worker(self, r: dict, usage, status: str, run_dir: Path) -> None:
        db = self.p.db
        task = db.task(r["task"]) if r["task"] else None
        if not task:
            return
        result = _read_result(run_dir / RESULT_FILE) or last_json_object(usage.final_text or "") or {}
        rstatus = result.get("status") if isinstance(result, dict) else None
        summary = (result.get("summary") if isinstance(result, dict) else None) or (usage.final_text or "")[:1500]
        waiting = status == "ok" and rstatus == "waiting"
        if waiting:
            new = "queued"   # a busy resource is not a failed attempt: the task comes back later
        elif status == "ok" and rstatus in ("done", "blocked", "failed", "needs_review", None):
            new = {"done": "done", "blocked": "blocked", "failed": "failed", "needs_review": "review",
                   None: "done"}[rstatus]
        elif status in ("limit", "auth"):
            new = "queued"   # not an attempt: the account refused, the task did not fail
        else:
            new = "failed"
        attempts = int(task["attempts"] or 0) + (0 if status in ("limit", "auth") or waiting else 1)
        if new == "failed" and attempts < int(task["max_attempts"] or 3) and status in ("failed", "lost", "timeout",
                                                                                     "stalled"):
            new = "queued"
        extra: dict = {}
        reason = None
        not_before = None
        if waiting:
            try:
                waits = int(json.loads(task["result"] or "{}").get("waits") or 0) + 1
            except (ValueError, AttributeError):
                waits = 1
            what = str(result.get("waiting_for") or summary)[:300]
            extra["waits"] = waits
            if waits > int(self.cfg["budget"].get("max_waits", 24)):
                new, reason = "blocked", f"still waiting after {waits} tries: {what}"
            else:
                retry = min(max(float(result.get("retry_after_s") or 1800), 300.0), 6 * 3600.0)
                not_before = time.time() + retry
                reason = f"waiting for {what}; next try {time.strftime('%H:%M', time.localtime(not_before))}"
        upd = {"status": new, "attempts": attempts, "result": json.dumps(
            {"summary": summary, "status": rstatus or status, **extra,
             **({k: v for k, v in result.items() if k not in ("summary", "waits")}
                if isinstance(result, dict) else {})})[:20000]}
        if reason:
            upd["blocked_reason"] = reason[:500]
        elif new == "blocked":
            upd["blocked_reason"] = (result.get("question") or result.get("blocked_reason") or summary)[:500]
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
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), f"task:{task['id']}", f"task_{new}", sev,
              f"#{task['id']} {task['title']} → {new} (run {status}, ${usage.cost_usd:.2f}): {summary[:1200]}",
              "queued", task["id"]))
        for f in (result.get("followups") or [])[:5] if isinstance(result, dict) else []:
            if isinstance(f, dict) and f.get("title"):
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
        windows = bud.windows_from_snapshots(self.p.db)
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
            if prev and prev.level != g.level and not provider_paused:   # a pause has its own, specific alert
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
        if not msgs and not evs:
            busy = db.one("SELECT id FROM tasks WHERE status IN ('queued','running')")
            last = float(db.kv("last_coordinator_turn", 0))
            gate = self.gates.get(self.cfg.get("core_provider", "claude"))
            idle_due = (not busy and now - last > float(c.get("idle_wake_s", 1800))
                        and (gate is None or gate.allow_optional))
            if not idle_due:
                return
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
        prompt = coord.digest(self.p, gates, [e["id"] for e in evs], [m["id"] for m in msgs])
        default_chat = msgs[-1]["chat"] if msgs else None
        provider = self.cfg.get("core_provider", "claude")
        self.start_run("coordinator", prompt, provider, c.get("tier", "light"), str(self.p.base),
                       read_only=True, schema=coord.ACTIONS_SCHEMA, system=coord.system_prompt(self.p),
                       budget_usd=float(c.get("turn_budget_usd", 1.0)), timeout_s=float(c.get("turn_timeout_s", 600)),
                       note={"messages": [m["id"] for m in msgs], "events": [e["id"] for e in evs],
                             "default_chat": default_chat})
        db.set_kv("last_coordinator_turn", now)

    # workers ----------------------------------------------------------------------------------------
    def dispatch(self) -> None:
        db = self.p.db
        running = db.q("SELECT provider, COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running' "
                       "GROUP BY provider")
        busy = {r["provider"]: r["n"] for r in running}
        for task in db.ready_tasks():
            provider = task["provider"] or self.cfg.get("core_provider", "claude")
            gate = self.gates.get(provider) or bud.evaluate(db, self.cfg, provider, bud.windows_from_snapshots(db))
            if not gate.allow_new_work or busy.get(provider, 0) >= gate.max_parallel:
                continue
            if task["origin"] in ("schedule", "harness") and not gate.allow_optional:
                continue
            if not self._resources_free(task):
                continue
            remaining = (task["budget_usd"] or 0) - (task["spent_usd"] or 0)
            if task["budget_usd"] and remaining <= 0.05:
                db.update_task(task["id"], status="blocked", blocked_reason="task budget exhausted")
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), "daemon", "task_budget_exhausted", "normal",
                      f"#{task['id']} {task['title']} used its ${task['budget_usd']:.2f} budget", "queued", task["id"]))
                continue
            tier = bud.clamp_tier(task["tier"], gate)
            try:
                cwd, branch = self._workdir_for(task)
            except Exception as e:
                db.update_task(task["id"], status="blocked", blocked_reason=f"workspace: {e}"[:400])
                continue
            from .prompts import worker_prompt
            prompt = worker_prompt(self.p, task, cwd, branch)
            db.update_task(task["id"], status="running", branch=branch, blocked_reason=None)
            self.start_run("worker" if task["kind"] != "review" else "reviewer", prompt, provider, tier, cwd,
                           task=task, budget_usd=max(remaining, 0.5) if task["budget_usd"] else None,
                           read_only=False)
            busy[provider] = busy.get(provider, 0) + 1

    def _resources_free(self, task: dict) -> bool:
        """Tasks labelled `resource:<name>` share that resource's slot count (config `resources`),
        e.g. one device: at most `resources.device` such tasks run at once."""
        wanted = [lb.split(":", 1)[1] for lb in json.loads(task["labels"] or "[]") if lb.startswith("resource:")]
        limits = self.cfg.get("resources", {})
        for res in wanted:
            limit = int(limits.get(res, 1))
            busy = self.p.db.one("SELECT COUNT(*) n FROM tasks WHERE status='running' AND labels LIKE ?",
                                 (f'%"resource:{res}"%',))["n"]
            if busy >= limit:
                return False
        return True

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


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
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
