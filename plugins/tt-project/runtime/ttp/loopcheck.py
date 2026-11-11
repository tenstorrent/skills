# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp doctor --loop <provider>`: one tiny code task end to end on a real agent CLI. Opt-in, and it
spends money (capped at CAP_USD), so the tests run it only with the fake provider.

It makes a scratch project under the tt-project home (registered nowhere, no service installed; Jev,
chat notices and schedules off) whose repository's only remote is a local bare repository: nothing
reaches a real remote. It queues one light code task (add a line to NOTES.md and land it on the push
branch) and ticks one daemon process of its own, as the service does: a waiting task's background
probe is read back on a later tick of the same daemon. The daemon dispatches the task, queues its review
itself (review.auto; a needs_review hand-off's from its review sweep, run each tick here, as the service's
waits review.sweep_after_s behind the coordinator) and pushes the approved head through its push queue
(no model). The task asks the worker to commit and not push: the review and push queue land it. It stops once the
line is on the bare remote's push branch and the review is closed, a step fails, the wall clock
(CAP_S) runs out or the runs spent CAP_USD; then it stops any run still going and removes the project.
A check killed before that leaves its project behind; the next check stops its runs and removes it.

The coordinator does not run (`--live` measures its turn), nor any tick step that reaches beyond the
scratch project (STUBBED: releases, upstream notes, chat, machine-wide claims, backups, replicas).
"""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

NAME = "loop-check"
BRANCH = "loop-check"     # the push branch on the bare remote
CAP_S = 900               # the whole loop's wall clock
CAP_USD = 1.0             # all runs together; a run still going when it is passed is stopped
TASK_USD = 0.5            # each task's budget (budget.task_default_usd for light)
TICK_S = 5.0              # between ticks
TICK_TIMEOUT_S = 180
DONE = "ttp-loop-tick:"   # the tick process's answer line
STOP_WAIT_S = 30          # for stopped runs to end before the project is removed
STUBBED = ("maybe_coordinate", "run_schedules", "review_jev", "check_release", "poll_slack", "deliver_outbound",
           "read_upstream", "wake_upstream_fixes", "forward_upstream", "drain_note_outbox", "remind_logouts",
           "relay_when_blocked", "replicate_state", "backup_branches", "check_notifier", "check_linger",
           "route_machines", "check_unreachable", "sync_shared_pauses")
ENDED = ("failed", "blocked", "cancelled")
# A run's identity, when the check itself runs inside one: the scratch project's runs get their own.
RUN_VARS = ("TTP_RUN_DIR", "TTP_TASK", "TTP_TASK_ID", "TTP_RUN_ID", "TTP_PROJECT", "TTP_PIDNS", "TTP_CGROUP",
            "TTP_LOCKS_HELD", "TTP_DETACHED", "TTP_OUTLIVES_RUN")


def ticker(base: str, cap_usd: float = CAP_USD) -> None:
    """One daemon with the STUBBED steps off, ticking once per line read from stdin and answering with
    a DONE line. One process, as in the service: a waiting task's background probe and an on_pass
    check are read back on a later tick of the same daemon (fresh-process ticks lost them all).
    It dispatches nothing once the runs spent more than `cap_usd`: a run ending in a tick can queue
    a review that the same tick would start, past the cap run() checks between ticks."""
    from .daemon import Daemon
    d = Daemon(base)
    for name in STUBBED:
        setattr(d, name, lambda *a, **k: None)
    dispatch = d.dispatch

    def capped() -> None:
        if float(d.p.db.one("SELECT COALESCE(SUM(cost_usd),0) s FROM runs")["s"] or 0) <= cap_usd:
            dispatch()
    d.dispatch = capped
    for _ in sys.stdin:
        d._review_swept = 0   # the review sweep runs each tick (REVIEW_SWEEP_EVERY_S in the service)
        try:
            d.tick()
            print(f"{DONE} ok", flush=True)
        except Exception as e:
            print(f"{DONE} {type(e).__name__}: {e}".replace("\n", " ")[:400], flush=True)


class Ticker:
    """The check's one daemon process; tick() waits up to TICK_TIMEOUT_S for its answer."""

    def __init__(self, base: Path, cap_usd: float = CAP_USD):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from ttp import loopcheck; "
                "loopcheck.ticker(sys.argv[2], float(sys.argv[3]))")
        env = {k: v for k, v in os.environ.items() if k not in RUN_VARS}
        self.proc = subprocess.Popen([sys.executable, "-c", code, str(Path(__file__).resolve().parent.parent),
                                      str(base), repr(float(cap_usd))], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        self.buf, self.tail = b"", []

    def tick(self) -> None:
        self.proc.stdin.write(b"tick\n")
        self.proc.stdin.flush()
        deadline = time.time() + TICK_TIMEOUT_S
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                text = line.decode(errors="replace")
                if text.startswith(DONE):
                    if text[len(DONE):].strip() != "ok":
                        raise RuntimeError(f"tick raised {text[len(DONE):].strip()}")
                    return
                self.tail = (self.tail + [text])[-10:]
            left = deadline - time.time()
            if left <= 0:
                raise subprocess.TimeoutExpired("daemon tick", TICK_TIMEOUT_S)
            if select.select([self.proc.stdout], [], [], left)[0]:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError(f"tick process exited {self.proc.wait()}: {' / '.join(self.tail)[-400:]}")
                self.buf += chunk

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=STOP_WAIT_S)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait()


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout.strip()


def setup(tmp: Path, provider: str, word: str):
    """The scratch project: repo/ with NOTES.md, its origin the bare remote.git holding main and BRANCH."""
    from .cli import bootstrap
    from .project import write_json
    remote, repo = tmp / "remote.git", tmp / "repo"
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    _git("init", "-q", "-b", "main", str(repo))
    (repo / "NOTES.md").write_text("# Notes\n")
    _git("add", "NOTES.md", cwd=repo)
    _git("-c", "user.name=ttp", "-c", "user.email=ttp@localhost", "commit", "-qm", "notes", cwd=repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "origin", "main", f"main:{BRANCH}", cwd=repo)
    _git("fetch", "-q", "origin", cwd=repo)
    p = bootstrap(repo, NAME, "Run one tiny code task end to end.", provider)
    cfg = json.loads(p.config_path.read_text())
    cfg.update({"jev": {"enabled": False}, "notify": {"slack": False},
                "delivery": {"push_branch": BRANCH, "base_ref": "origin/main", "push_allowed": True,
                             "push_queue": True, "push_batch_s": 0, "push_min_gap_s": 0,
                             "push_checks": ["git diff --check origin/main...HEAD"]},
                "review": {**cfg.get("review", {}), "sweep_after_s": 1},
                "budget": {**cfg.get("budget", {}), "task_default_usd": {"light": TASK_USD, "standard": TASK_USD,
                                                                         "deep": TASK_USD}}})
    write_json(p.config_path, cfg)
    write_json(p.harness / "schedules.json", [])
    p.db.x("UPDATE messages SET handled=1")   # the "project created" note is the coordinator's
    spec = (f"Add the line `loop check {word}` at the end of NOTES.md in the repository and commit it on your "
            f"branch. Do not push: the review and push queue put it on {BRANCH}. Change nothing else.")
    tid = p.db.add_task("Add a line to NOTES.md", spec, kind="code", tier="light", origin="user",
                        provider=provider)
    return p, remote, tid


class Watch:
    """Reads the scratch project after each tick and reports each new step once."""

    def __init__(self, p, remote: Path, tid: int, word: str, emit: Callable[[str], None]):
        self.p, self.remote, self.tid, self.word, self.emit = p, remote, tid, word, emit
        self.seen: set[str] = set()
        self.t0 = time.time()
        self.steps: dict[str, bool] = {}

    def spent(self) -> float:
        return float(self.p.db.one("SELECT COALESCE(SUM(cost_usd),0) s FROM runs")["s"] or 0)

    def say(self, key: str, step: str, text: str, ok: bool = True) -> None:
        if key in self.seen:
            return
        self.seen.add(key)
        self.steps[step] = self.steps.get(step, True) and ok
        self.emit(f"{time.time() - self.t0:5.0f}s  {'' if ok else 'FAIL '}{step:<8} {text} · "
                  f"spent ${self.spent():.2f}")

    def landed(self) -> str:
        try:
            log = _git("--git-dir", str(self.remote), "log", "--format=%H", BRANCH, "--", "NOTES.md")
            notes = _git("--git-dir", str(self.remote), "show", f"{BRANCH}:NOTES.md")
        except subprocess.CalledProcessError:
            return ""
        return log.splitlines()[0] if log and f"loop check {self.word}" in notes else ""

    def _runs(self, task: int, step: str) -> str | None:
        """Report the task's runs; the status of an auth failure, else None."""
        for r in self.p.db.q("SELECT * FROM runs WHERE task=? ORDER BY id", (task,)):
            self.say(f"start{r['id']}", "dispatch", f"task #{task} run #{r['id']} on {r['provider']}"
                     + (f" {r['model']}" if r["model"] else "") + (f" ({r['effort']})" if r["effort"] else ""))
            if r["status"] == "running":
                continue
            try:
                res = json.loads((Path(r["dir"] or self.p.runs / str(r["id"])) / "result.json").read_text())
            except (OSError, ValueError):
                res = {}
            ok = r["status"] in ("ok", "done") and bool(res)
            self.say(f"end{r['id']}", step, f"run #{r['id']} {r['status']}, "
                     + (f"hand-off {res.get('status')}: {str(res.get('summary') or '')[:120]}" if res
                        else "no hand-off") + f" · run ${float(r['cost_usd'] or 0):.2f}", ok)
            if r["status"] == "auth":
                return "auth"
        return None

    def check(self) -> str | None:
        """None while the loop goes on; "pass", "auth" or a failure's reason once it ended."""
        db = self.p.db
        task = db.task(self.tid)
        if self._runs(self.tid, "hand-off") == "auth":
            return "auth"
        if task["status"] in ENDED:
            self.say("task-end", "hand-off", f"task #{self.tid} {task['status']}", False)
            return f"the code task {task['status']}"
        reviews = [t for t in db.q("SELECT * FROM tasks WHERE kind='review' ORDER BY id")]
        for rv in reviews:
            self.say(f"review{rv['id']}", "review", f"review #{rv['id']} queued ({rv['tier']})")
            if self._runs(rv["id"], "review") == "auth":
                return "auth"
            if rv["status"] in ENDED:
                self.say(f"review-end{rv['id']}", "review", f"review #{rv['id']} {rv['status']}", False)
                return f"review #{rv['id']} {rv['status']}"
        queue = db.q("SELECT * FROM push_queue ORDER BY id")
        for q in queue:
            self.say(f"approved{q['id']}", "review", f"review #{q['task']} passed: approved {q['head'][:12]} "
                     f"for {q['target']}")
            if q["batch"]:
                self.say(f"batch{q['id']}", "push", f"batch {q['batch']} started for {q['head'][:12]}")
            if q["status"] in ("pushed", "landed") or q["pushed_sha"]:
                self.say(f"pushed{q['id']}", "push", f"pushed {str(q['pushed_sha'] or '')[:12]} to {BRANCH}")
            elif q["status"] in ("failed", "conflict", "cancelled", "rejected"):
                self.say(f"push-end{q['id']}", "push", f"{q['status']}: {str(q['detail'] or '')[:160]}", False)
                return f"the push {q['status']}"
        sha = self.landed()
        if sha:
            self.say("landed", "landed", f"{sha[:12]} on {BRANCH} of the bare remote has the line")
            if not any(q["batch"] for q in queue):
                # The line reached the remote before any review approved it or the push queue pushed it.
                self.say("bypass", "landed", "the change reached the remote outside the review and push queue",
                         False)
                return "landed without the review and push queue"
            open_ = [rv["id"] for rv in reviews if rv["status"] not in ("done",) + ENDED]
            if not open_ and self.steps.get("push") and self.steps.get("review"):
                return "pass"
        return None


def say(line: str) -> None:
    """One step line, flushed: a detached check's log shows each step as it happens."""
    print(line, flush=True)


def sweep(home: Path) -> None:
    """Remove the scratch projects of a check killed before its cleanup, once past any check's wall clock."""
    from .project import Project
    for old in home.glob("loop-check-*"):
        try:
            if time.time() - old.stat().st_mtime < 2 * CAP_S:
                continue
            if (old / "repo" / "tt-project" / "state").is_dir():
                stop_all(Project(old / "repo"))
        except Exception:   # a half-made scratch project: nothing in it to stop
            pass
        shutil.rmtree(old, ignore_errors=True)


def stop_all(p) -> None:
    """Stop every run still going and wait briefly for each to end, then end the detached checks
    still running in the scratch project (the daemon's prechecks, a run's own): left alone they run
    on past the check and write into its folder after it is removed."""
    from .runner import stop_runs
    ids = stop_runs(p.db, p.runs, why="loop check over")
    deadline = time.time() + STOP_WAIT_S
    for rid in ids:
        exit_file = p.runs / str(rid) / "exit.json"
        while not exit_file.exists() and time.time() < deadline:
            time.sleep(1)
    stop_checks(p)


def stop_checks(p) -> None:
    """End each detached `ttp checks` the scratch project started (its process group: TERM, then
    KILL) and wait for it to exit."""
    from .cli import _checks_alive, _read_checks_pid
    from .daemon import PRECHECKS_DIR
    dirs = []
    for top in (p.state / PRECHECKS_DIR, p.runs):
        try:
            dirs += [d for d in top.iterdir() if d.is_dir()]
        except OSError:
            pass
    live = [i for i in map(_read_checks_pid, dirs) if _checks_alive(i)]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for info in live:
            try:
                os.killpg(info["pid"], sig)
            except OSError:
                pass
        deadline = time.time() + STOP_WAIT_S / 2
        while live and time.time() < deadline:
            time.sleep(0.1)
            live = [i for i in live if _checks_alive(i)]
        if not live:
            return


def run(provider: str, *, home: Path | None = None, cap_s: float = CAP_S, cap_usd: float = CAP_USD,
        tick_s: float = TICK_S, emit: Callable[[str], None] = say) -> dict:
    """{"provider", "available", "reason", "outcome" ("pass" or why it stopped), "spend_usd", "steps"}."""
    import uuid
    from . import project
    from .live import unavailable
    from .providers import get_provider
    out = {"provider": provider, "available": False, "reason": "", "outcome": "", "spend_usd": 0.0, "steps": {}}
    out["reason"] = unavailable(get_provider(provider))
    if out["reason"]:
        emit(f"{provider}: not available: {out['reason']}")
        return out
    out["available"] = True
    home = Path(home or project.HOME_DIR)
    home.mkdir(parents=True, exist_ok=True)
    sweep(home)
    tmp = Path(tempfile.mkdtemp(prefix="loop-check-", dir=home))
    word = uuid.uuid4().hex[:8]
    p = ticker = None
    try:
        p, remote, tid = setup(tmp, provider, word)
        w = Watch(p, remote, tid, word, emit)
        w.emit(f"    0s  queued   task #{tid}: add `loop check {word}` to NOTES.md, land it on {BRANCH}")
        outcome = None
        ticker = Ticker(p.base, cap_usd)
        while outcome is None:
            try:
                ticker.tick()
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                outcome = f"tick failed: {str(e)[:300]}"
                break
            outcome = w.check()
            if outcome is None and w.spent() > cap_usd:
                outcome = f"spend cap ${cap_usd:g} reached"
            elif outcome is None and time.time() - w.t0 > cap_s:
                outcome = f"time cap {cap_s:.0f}s reached"
            elif outcome is None:
                time.sleep(tick_s)
        if outcome == "auth":
            out["available"], out["reason"] = False, f"logged out ({get_provider(provider).login_hint})"
        out["outcome"], out["steps"], out["spend_usd"] = outcome, w.steps, round(w.spent(), 4)
        emit(f"{time.time() - w.t0:5.0f}s  {'pass' if outcome == 'pass' else 'FAIL'}     "
             + ("the change landed after review" if outcome == "pass" else outcome)
             + f" · spent ${w.spent():.2f}")
    finally:
        if ticker is not None:
            ticker.close()
        if p is not None:
            stop_all(p)
        for _ in range(STOP_WAIT_S):   # a stopped run's runner may still write its last files
            shutil.rmtree(tmp, ignore_errors=True)
            if not tmp.exists():
                break
            time.sleep(1)
    return out
