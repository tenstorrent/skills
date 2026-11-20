# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Terminal-Bench cycles: the served chat models' paced, model-free Terminal-Bench runs
(`ttp model-bench terminalbench`), the ranking's first key (modelscore.TB) and each model's verdict.

A cycle is one pass of a model over the task list (harborslice.tasks). step() (the bench owner's
hourly command schedule modelbench.TB_SCHEDULE, or by hand) books every complete cycle and starts one
detached runner per served model that has work and none running; it calls no model. A runner holds
the model's lock (one trial per model at once), runs its cycle's next unrun task through
harborslice.trial (the host-wide trial cap, 429 waits, resume), appends the result to the cycle's
<model>.jsonl and waits the pace gap (cycle_h spread over the task count) before the next, so a cycle
takes about cycle_h and other users keep the endpoint. A new cycle starts cycle_h after the last one
started. A model first seen less than new_model_h ago waits; then it runs at the same pace, so its
first run lands within the day. A restart loses nothing: a runner resumes the cycle's next task and a
half-run Harbor job resumes inside harborslice.trial.

The scorer (book) reads the cycle files and appends one suite "terminalbench" row per complete cycle
to model-bench/<date>.jsonl (modelscore.bench reads it), with effort, agent, dataset and cycle; a model
keeps its last complete score until its next cycle completes. Verdicts follow modelsuite.verdict over
each model's latest complete row, booked through modelbench.record (source "terminalbench"). State:
<modelbench.home()>/terminalbench/state.json; trials under terminalbench/cycles/<n>/<model>/<task>.
Settings: model_bench.terminalbench {cycle_h, new_model_h} beside harborslice.settings.

Reference (`ttp model-bench terminalbench --reference`): a one-time run of fixed reference models (setting
reference.models, default REF_MODELS) at reference.effort (default medium) over the full task list, through
the Anthropic-capable endpoint saved under the alias reference.endpoint (no default) whose URL and key reach
only the child's environment. Started by hand only: step() and the schedule never start it. Each model runs
its tasks one at a time (ref_runner, resumable) and books one row in terminalbench/reference.jsonl once; a
booked model never runs again. Those rows stay out of the ranking (modelscore.bench never reads them) and
are shown beside it marked as reference (reference_view); with none booked, the SUBSET_REFERENCE rows of the
DEFAULT_TASKS subset are shown, marked as such.

Reference routes (a same-conditions comparison): a model listed in reference.bridge is called through the
logged-in claude CLI (claudebridge.ClaudeCliBridge, a loopback server in the runner's own process that the
agent reaches as OPENAI_BASE_URL; no API key, no endpoint), at its effort (reference.efforts[model], else
reference.effort); its row carries route "claude-cli", the CLI version and the dollars it reported. Every
other model goes through the endpoint and its runner takes `ttp lock <reference.lock>` (default LOCK, ""
for none) before each trial (EndpointLock), so one endpoint model runs at a time; a stay-off wait frees it. reference.parallel (default 1) runs that many of a
model's tasks at once. limit_s 0 (harborslice) gives every model its task's own limit. A model listed in
reference.chat goes through the project's chat endpoint (providers.chat; no reference endpoint needed) by a
loopback relay in the runner (one per trial, like the bridge) that times every call, under the same lock; the
relay adds the effort's request fields (providers.chat.effort_fields, as a chat worker at that effort gets) and
the table's how-called line names them (parity request).
reference.run names a comparison run: its trials, rows and table live in terminalbench/<run> (ref_dir) and
reference.note is printed in its table.

Server verdicts (serverhealth): a chat-route trial cut at its limit (or refused) while the server answered
slower than this model's normal speed (server-history.json) is held out as outcome "server", never failed;
a cut made before the normal speed was known is judged again once it is (rejudge). Once the pool is done,
each held-out task is rerun one at a time after a probe shows the server healthy (probe_every_s up to
probe_max_s), at most reference.rounds times, with the same limit. The run books each row once nothing is
owed and writes terminalbench/<run>/table.md (table): passed of scored per model, the server-held tasks, the
cut-offs with a server-or-model verdict each, median server wait and tokens/s, Claude dollars, the limit,
benchparity.section (same task set, limit and rerun rule, and how each arm was called) and the note.

Pre-flight (preflight): a cycle that cannot start (no working endpoint key, no harbor venv, no docker
compose) used to fail only in terminalbench.log. Each failure is counted in the bench owner's streak of
TB_SCHEDULE (modelbench.streak, which also counts a step that crashed) and a pass resets it; the
PREFLIGHT_FAILS-th (modelbench.STREAK_FAILS) failure in a row raises one observation
event (source "model-bench", one per streak, modelbench.streak) naming the cause, so the owner fixes it the
same day.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import schedjobs

DIR, STATE, CYCLE_H, NEW_MODEL_H = "terminalbench", "state.json", 24.0, 3.0
NOT_SERVED = "server: model not served"   # a chat row whose model the endpoint does not list: ended, not run (server)
MISSING_MAX = 3   # model_missing answers a trial's relay passes on before it answers the 404 itself (Bridge.missing_max)
NOT_RUN = "not_run"   # a bridge or chat trial with no model call or broken by failed calls (not_run): rerun, shown as not run
RUNNER, REF_RUNNER, STOP_REASON = "terminalbench runner", "terminalbench reference runner", "bench.stop"
REF, REF_FILE, REF_MODELS, REF_EFFORT = "reference", "reference.jsonl", ("claude-opus-5-5", "claude-sonnet-5-5"), "medium"
# Shown until a reference run is booked: an earlier run of the DEFAULT_TASKS subset (Terminus 2, 900 s per task).
SUBSET_REFERENCE = ({"model": "Opus 5.5", "effort": "low", "passed": 14, "trials": 20},
                    {"model": "Sonnet 5.5", "effort": "low", "passed": 14, "trials": 20})
LOCK, BRIDGE_ROUTE, CHAT_ROUTE, ENDPOINT_ROUTE = "chat-server", "claude-cli", "chat", "endpoint"
KIND = {BRIDGE_ROUTE: "bridge: the logged-in claude CLI (claude -p) through a loopback server, no API key",
        CHAT_ROUTE: "endpoint: the chat endpoint (providers.chat) through a loopback relay that times each call",
        ENDPOINT_ROUTE: "endpoint: the saved reference endpoint, called directly"}
PROVIDER = {BRIDGE_ROUTE: "claude", CHAT_ROUTE: "chat", ENDPOINT_ROUTE: "endpoint"}
ROUNDS, PROBE_EVERY_S, PROBE_MAX_S, HISTORY = 3, 120.0, 3600.0, "server-history.json"
RESERVED = {REF, "cycles", "harbor", "compose"}
PREFLIGHT_FAILS = 3   # modelbench.STREAK_FAILS
SUBSET_NOTE = "20-task subset (2026-10-27, low effort, 900 s per task)"


def settings(cfg: dict) -> dict:
    from . import harborslice
    mb = cfg.get("model_bench") if isinstance(cfg.get("model_bench"), dict) else {}
    s = mb.get("terminalbench") if isinstance(mb.get("terminalbench"), dict) else {}

    def hours(k, d):
        v = s.get(k, d)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else d
    r = s.get(REF) if isinstance(s.get(REF), dict) else {}
    ms = r.get("models")
    eff = str(r.get("effort") or REF_EFFORT)
    br, ef = r.get("bridge"), r.get("efforts")
    par, rd, run = r.get("parallel"), r.get("rounds"), str(r.get("run") or "")
    ch = r.get("chat")

    def secs(k, d):
        v = r.get(k, d)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else d
    ref = {"endpoint": str(r.get("endpoint") or ""),
           "models": [str(m) for m in ms if str(m).strip()] if isinstance(ms, list) else list(REF_MODELS),
           "effort": eff if eff.isalpha() else REF_EFFORT,   # it goes into the harbor command line
           "bridge": [str(m) for m in br if str(m).strip()] if isinstance(br, list) else [],
           "efforts": {str(k): str(v) for k, v in ef.items() if str(v).isalpha()} if isinstance(ef, dict) else {},
           "parallel": int(par) if isinstance(par, int) and not isinstance(par, bool) and par >= 1 else 1,
           "chat": [str(m) for m in ch if str(m).strip()] if isinstance(ch, list) else [],
           "run": run if run.replace("-", "").replace("_", "").replace(".", "").isalnum() and run not in RESERVED
           and not run.startswith(".") else "",
           "note": str(r.get("note") or ""),
           "rounds": int(rd) if isinstance(rd, int) and not isinstance(rd, bool) and rd >= 0 else ROUNDS,
           "probe_every_s": secs("probe_every_s", PROBE_EVERY_S), "probe_max_s": secs("probe_max_s", PROBE_MAX_S),
           "lock": str(r["lock"]) if isinstance(r.get("lock"), str) else LOCK}
    return {**harborslice.settings(cfg), "cycle_h": max(hours("cycle_h", CYCLE_H), 1.0),
            "new_model_h": hours("new_model_h", NEW_MODEL_H), REF: ref}


def preflight(p, cause: str = "", now: float | None = None) -> int:
    """Count a cycle's pre-flight in modelbench.streak (schedule TB_SCHEDULE): a failure
    (`cause`, one line) adds one, a pass (empty cause) resets the count; the PREFLIGHT_FAILS-th failure in a row
    queues one observation event naming the cause (one per streak). Returns the streak length."""
    from . import modelbench
    return modelbench.streak(p, modelbench.TB_SCHEDULE, cause, now, what="Terminal-Bench cycles failed their pre-flight")

def folder() -> Path:
    from . import modelbench
    return modelbench.home() / DIR


def compose(cfg: dict, env: dict | None = None) -> tuple[dict | None, str]:
    """harborslice.compose from the bench's own folder: a compose binary in folder()/compose (a name kept out
    of the run folders by RESERVED) needs no setting, and the docker config link lives beside it."""
    from . import harborslice
    return harborslice.compose(folder(), settings(cfg), env)


def _load() -> dict:
    try:
        v = json.loads((folder() / STATE).read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    from .project import durable_write
    folder().mkdir(parents=True, exist_ok=True)
    durable_write(folder() / STATE, json.dumps(st, indent=1, sort_keys=True))


def _locked(path: Path):
    """The open, flock'd file at `path`, or None while another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def results(cyc: dict, model: str) -> dict[str, dict]:
    """{task: result} of `model`'s cycle `cyc`, from its <model>.jsonl (later lines win)."""
    from .harborslice import slug
    return _rows(folder() / "cycles" / str(cyc["n"]) / f"{slug(model)}.jsonl", cyc["tasks"])


def _rows(path: Path, tasks: list[str] | None = None) -> dict[str, dict]:
    """{task: result} of a results file (later lines win); `tasks` None: the dict rows themselves, by line."""
    out: dict = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for i, ln in enumerate(lines):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and (tasks is None or r.get("task") in tasks):
            out[i if tasks is None else r["task"]] = r
    return out


def book(cfg: dict, st: dict, now: float) -> list[dict]:
    """Append one row per newly complete cycle (module doc) and record verdicts; returns the new rows."""
    from . import harborslice, modelbench, modelsuite
    s, new = settings(cfg), []
    for m, ms in sorted((st.get("models") or {}).items()):
        cyc = ms.get("cycle")
        if not cyc or cyc.get("booked"):
            continue
        got = results(cyc, m)
        if len(got) < len(cyc["tasks"]):
            continue
        sc = harborslice.score([got[t] for t in cyc["tasks"]])
        row = {"date": modelbench.day(now), "model": m, "class": "terminalbench", "suite": "terminalbench",
               "effort": modelsuite.effort(cfg, "standard"), "agent": s["agent"], "dataset": s["dataset"],
               "cycle": cyc["n"], "started": cyc["started"], "tasks": len(cyc["tasks"]), "dry_run": False,
               "at": now, **sc, "score": sc["pass_rate"]}
        modelsuite._append(modelbench.home() / f"{row['date']}.jsonl", row)
        cyc["booked"], ms["last"] = now, {k: row[k] for k in ("date", "pass_rate", "passed", "trials", "faults", "cycle")}
        new.append(row)
    if new:
        latest = [{"model": m, "class": "terminalbench", **ms["last"]} for m, ms in st["models"].items()
                  if ms.get("last") and ms["last"].get("pass_rate") is not None]
        modelbench.record({r["model"]: v for r in new
                           if (v := modelsuite.verdict(latest, r["model"], "terminalbench", s["floor_share"]))},
                          now, "terminalbench")
    return new


def due(st: dict, cfg: dict, model: str, now: float) -> bool:
    """`model` has a task to run now: its cycle is unfinished, or the next one may start (module doc)."""
    s, ms = settings(cfg), (st.get("models") or {}).get(model) or {}
    if now - float(ms.get("first_seen") or now) < s["new_model_h"] * 3600:
        return False
    cyc = ms.get("cycle")
    return not cyc or not cyc.get("booked") or now - float(cyc["started"]) >= s["cycle_h"] * 3600


def step(cfg: dict, served: list[str], now: float, launch: Callable[[str], None]) -> dict:
    """The model-free scheduled pass: book complete cycles, then launch a runner per model that is due and
    has none running. Returns {"booked": [models], "launched": [models], "waiting": {model: why}}."""
    from .harborslice import slug
    lock = _locked(folder() / "state.lock")
    if lock is None:
        return {"booked": [], "launched": [], "waiting": {"*": "another step is running"}}
    try:
        st = _load()
        models = st.setdefault("models", {})
        for m in served:
            models.setdefault(m, {"first_seen": now})
        booked = [r["model"] for r in book(cfg, st, now)]
        _save(st)
    finally:
        lock.close()
    launched, waiting = [], {}
    for m in served:
        if not due(st, cfg, m, now):
            waiting[m] = "paced: cycle done or model newly served"
            continue
        if (held := _locked(folder() / "running" / f"{slug(m)}.lock")) is None:
            waiting[m] = "its runner is running"
            continue
        held.close()
        launch(m)
        launched.append(m)
    return {"booked": booked, "launched": launched, "waiting": waiting}


def _next(cfg: dict, home: Path, model: str, now: float) -> tuple[dict, str | None]:
    """(the model's cycle, its next unrun task or None), starting a new cycle when due; under the state lock."""
    from . import harborslice
    while (lock := _locked(folder() / "state.lock")) is None:
        time.sleep(1)
    try:
        st = _load()
        ms = st.setdefault("models", {}).setdefault(model, {"first_seen": now})
        cyc = ms.get("cycle")
        if not cyc or (cyc.get("booked") and now - float(cyc["started"]) >= settings(cfg)["cycle_h"] * 3600):
            n = int(st.get("next_cycle") or 1)
            st["next_cycle"] = n + 1
            ms["cycle"] = cyc = {"n": n, "started": now, "tasks": harborslice.tasks(home, settings(cfg))}
            _save(st)
    finally:
        lock.close()
    done = results(cyc, model)
    return cyc, next((t for t in cyc["tasks"] if t not in done), None)


def runner(cfg: dict, model: str, env: dict, *, trial: Callable | None = None, sleep: Callable = time.sleep,
           clock: Callable = time.time, stop: Callable[[], str] | None = None,
           gate: Callable | None = None) -> int:
    """Run `model`'s cycle to its end, one paced trial at a time (module doc); returns trials run. `stop`
    (modelbench.trial_stop) is asked before each trial: a reason ends the run there, the cycle kept. Each real
    trial first asks `ttp occupancy` for this host and waits while a live stream holds it (`gate`; an
    injected `trial` runs nothing here, so it is gated only when `gate` is given)."""
    from . import harborslice, modelbench, modelsuite, occupancy
    if gate is None and trial is None:
        gate = lambda: occupancy.wait_here("bench", "tbcycle", sleep=sleep, clock=clock)   # noqa: E731
    held = _locked(folder() / "running" / f"{harborslice.slug(model)}.lock")
    if held is None:
        return 0   # its runner is already running
    s, home, ran = settings(cfg), modelbench.home(), 0
    trial = trial or (lambda m, t, root: harborslice.trial(m, t, root, env, s, home=home))
    try:
        while True:
            cyc, task = _next(cfg, home, model, clock())
            if task is None:
                return ran
            gap = s["cycle_h"] * 3600 / max(len(cyc["tasks"]), 1)
            last = float(cyc.get("last_start") or 0)
            if (wait := last + gap - clock()) > 0:
                sleep(wait)
            if stop and stop():
                return ran
            if gate:   # a live stream on this host: the trial waits for it (occupancy.wait_free)
                gate()
            cyc["last_start"] = clock()
            got = trial(model, task, folder() / "cycles" / str(cyc["n"]))
            modelsuite._append(folder() / "cycles" / str(cyc["n"]) / f"{harborslice.slug(model)}.jsonl",
                               {"task": task, "at": clock(), **got})
            ran += 1
            _pace(model, cyc["n"], cyc["last_start"])
    finally:
        held.close()


def _pace(model: str, n: int, at: float) -> None:
    """Keep the cycle's last trial start in the state, so a restarted runner keeps the pace."""
    while (lock := _locked(folder() / "state.lock")) is None:
        time.sleep(1)
    try:
        st = _load()
        cyc = ((st.get("models") or {}).get(model) or {}).get("cycle") or {}
        if cyc.get("n") == n:
            cyc["last_start"] = at
            _save(st)
    finally:
        lock.close()


def ref_dir(run: str = "") -> Path:
    """Where a reference run keeps its trials and rows: terminalbench/reference, or terminalbench/<run> for a
    named comparison run (reference.run)."""
    return folder() / (run or REF)


def ref_file(run: str = "") -> Path:
    return ref_dir(run) / REF_FILE if run else folder() / REF_FILE


def route(s: dict, model: str) -> str:
    """How a reference model is called: the claude CLI bridge, the chat endpoint's relay or the reference endpoint."""
    return BRIDGE_ROUTE if model in s[REF]["bridge"] else CHAT_ROUTE if model in s[REF]["chat"] else ENDPOINT_ROUTE


def reference(run: str = "") -> list[dict]:
    """The booked reference rows, one per model (the first booked; module doc); `run`: a named run's."""
    out: dict[str, dict] = {}
    for r in _rows(ref_file(run)).values():
        if isinstance(r.get("model"), str):
            out.setdefault(r["model"], r)
    return list(out.values())


def reference_view() -> dict:
    """{"source": "reference" or "subset", "note", "rows"}: what the ranking shows beside it (module doc)."""
    rows = reference()
    if rows:
        return {"source": REF, "note": "fixed reference, run once, not ranked", "rows": rows}
    return {"source": "subset", "note": f"no reference run booked; the {SUBSET_NOTE}",
            "rows": [{**r, "pass_rate": round(r["passed"] / r["trials"], 3)} for r in SUBSET_REFERENCE]}


def reference_lines() -> list[str]:
    v = reference_view()
    out = [f"Terminal-Bench reference ({v['note']}):"]
    for r in v["rows"]:
        extra = f", agent {r.get('agent')}, {r.get('date')}, median {r.get('median_min')} min" if v["source"] == REF else ""
        out.append(f"  {r['model']} (effort {r.get('effort')}): {r.get('passed')}/{r.get('trials')} passed{extra}")
    return out


def ref_start(cfg: dict, served: list[str] | None, launch: Callable[[str], None]) -> dict:
    """Start a reference runner per reference model not booked, not running and served by the endpoint
    (`served` None: unknown, all)."""
    from .harborslice import slug
    s = settings(cfg)
    run = s[REF]["run"]
    booked = {r["model"] for r in reference(run)}
    got: dict[str, list] = {"launched": [], "booked": [], "running": [], "not_served": []}
    for m in s[REF]["models"]:
        if m in booked:
            got["booked"].append(m)
        elif served is not None and m not in served and route(s, m) == ENDPOINT_ROUTE:
            got["not_served"].append(m)
        elif (held := _locked(ref_dir(run) / f"{slug(m)}.lock")) is None:
            got["running"].append(m)
        else:
            held.close()
            launch(m)
            got["launched"].append(m)
    return got


def ref_command(effort: str) -> str:
    """The harbor command with the reference effort in the agent's call arguments."""
    from .harborslice import COMMAND
    return COMMAND.replace('{{"num_retries":8}}', '{{"num_retries":8,"extra_body":{{"reasoning_effort":"%s"}}}}' % effort)


def ref_effort(s: dict, model: str) -> str:
    return s[REF]["efforts"].get(model) or s[REF]["effort"]


def parity(s: dict, model: str, meta: dict, via: str, harbor: str) -> dict:
    """The conditions a reference row ran under (benchparity.FIELDS and how it was called): never a key or URL."""
    from . import benchparity
    lim = s["limit_s"]
    return {"harness": f"harbor {harbor}".strip(), "agent": s["agent"], "provider": PROVIDER[via], "route": via,
            "kind": KIND[via], "model": model, "effort": meta["effort"],
            "task_set": benchparity.task_set(meta["tasks"]), "tasks": len(meta["tasks"]),
            "limit_s": int(lim) if lim else "each task's own (task.toml)",
            "limit_counts": "harbor's agent timeout: the agent's wall time per task, model calls included",
            "rerun_rule": (f"a cut-off or capacity refusal while the server ran slow is not scored and rerun one at "
                           f"a time once a probe shows it healthy (up to {s[REF]['rounds']} reruns); no limit raise"
                           if via != ENDPOINT_ROUTE else "none: every trial is scored as it ran")}


class EndpointLock:
    """The shared endpoint lock (reference.lock) for a reference runner, held by a `ttp lock <name> -- <helper>`
    child whose helper only waits on its stdin, so taking it waits in line like any other `ttp lock` user (arrival
    order, pauses, the activity log) and closing that stdin frees it. take() before a trial (it stays held across
    trials), done() after; pause() frees it while no trial is in flight, during a stay-off wait that makes no model
    calls (occupancy.wait_free on_wait), and the next take() queues for it again. A dead runner's helper reads EOF
    and exits, freeing it."""
    READY = "ttp-endpoint-lock: held"

    def __init__(self, name: str, *, cwd=None, env: dict | None = None, sleep: Callable = time.sleep,
                 say: Callable[[str], None] | None = None):
        from threading import Lock
        self.name, self.cwd, self.env, self.sleep = name, cwd, env, sleep
        self.say = say or (lambda line: print(f"tbcycle: {line}", file=sys.stderr, flush=True))
        self.proc, self.busy, self.mu, self.takes, self.pauses = None, 0, Lock(), 0, 0

    def argv(self) -> list:
        helper = f"import sys; print({self.READY!r}, flush=True); sys.stdin.read()"
        return [sys.executable, "-m", "ttp", "lock", self.name, "--", sys.executable, "-c", helper]

    def take(self) -> None:
        with self.mu:
            self.busy += 1
            while self.proc is None:
                proc = subprocess.Popen(self.argv(), cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True)
                if any(line.strip() == self.READY for line in iter(proc.stdout.readline, "")):
                    self.proc, self.takes = proc, self.takes + 1
                    break
                rc = proc.wait()
                if rc != 75:   # 75: paused or busy; anything else is not a lock we can wait for
                    self.busy -= 1
                    raise RuntimeError(f"endpoint lock {self.name}: ttp lock exited {rc}")
                self.say(f"endpoint lock {self.name} paused or busy (ttp lock 75): asking again in 60 s")
                self.sleep(60)

    def done(self) -> None:
        with self.mu:
            self.busy = max(0, self.busy - 1)

    def pause(self) -> None:
        with self.mu:
            if self.proc is not None and not self.busy:
                self.say(f"stay-off wait: endpoint lock {self.name} freed until the next trial")
                self._free()
                self.pauses += 1

    def release(self) -> None:
        with self.mu:
            self._free()

    def _free(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.wait()


def ref_runner(cfg: dict, model: str, env: dict, *, trial: Callable | None = None, clock: Callable = time.time,
               bridge: Callable | None = None, chat: Callable | None = None, sleep: Callable = time.sleep,
               stop: Callable[[], str] | None = None, gate: Callable | None = None,
               rerun_server: int = 0, lock: EndpointLock | None = None,
               listed: Callable[[str], bool | None] | None = None) -> int:
    """Run `model`'s reference tasks (resuming), then book its row once; returns trials run. A booked model
    runs nothing (module doc). A bridge model gets the claude CLI's loopback server (bridge(model, effort)
    builds it), a chat model the chat endpoint's relay (chat(model)), one per trial so its server wait and
    tokens/s are the trial's own; reference.parallel tasks run at once. A trial cut while the server ran slow
    is `server` (serverhealth.judge): not scored, rerun one at a time once a probe shows the server healthy.
    `stop` (modelbench.trial_stop) is asked before each trial: a reason starts no more, nothing is booked.
    Capacity refusals over 20% lower the trials at once (benchbackoff). `rerun_server` N first reruns every
    task whose latest row is `server` (not scored), N at once, its new row replacing the old one.
    An endpoint or chat model takes reference.lock itself (EndpointLock, `lock` in tests) before each trial
    and frees it during a stay-off wait (`gate(on_wait)`), which makes no model calls.
    A chat model the endpoint's model list leaves out (`listed(model)` False: the relay's GET /models, as the
    served-models check reads it) runs nothing; one a trial's relay finds missing mid-row (model_missing) starts
    no more trials and no reruns, each relay passing on at most MISSING_MAX such answers. Either way meta gets
    `not_run` NOT_SERVED (the table's state) and nothing is booked; a later start that finds it listed clears it."""
    import shutil
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from . import benchbackoff, harborslice, modelbench, modelsuite, occupancy, serverhealth
    from .project import durable_write
    if gate is None and trial is None:   # each real trial asks `ttp occupancy` for this host first (runner)
        gate = lambda on_wait=None: occupancy.wait_here("bench", "tbcycle", sleep=sleep, clock=clock,   # noqa: E731
                                                        on_wait=on_wait)
    s = settings(cfg)
    run = s[REF]["run"]
    if any(r["model"] == model for r in reference(run)):
        return 0
    d, sl = ref_dir(run), harborslice.slug(model)
    d.mkdir(parents=True, exist_ok=True)
    held = _locked(d / f"{sl}.lock")
    if held is None:
        return 0   # its reference runner is already running
    home, ran = modelbench.home(), 0
    via = route(s, model)
    if lock is None and trial is None and s[REF]["lock"] and via != BRIDGE_ROUTE:   # one endpoint model at a time
        lock = EndpointLock(s[REF]["lock"], sleep=sleep)
    try:
        meta = json.loads((d / f"{sl}.json").read_text())
    except (OSError, ValueError):
        meta = {"started": clock(), "tasks": harborslice.tasks(home, s), "effort": ref_effort(s, model), "route": via,
                "model": model}
        meta["parity"] = parity(s, model, meta, via, harborslice.version(env))
        if via == CHAT_ROUTE:   # what the relay adds for the effort (providers.chat.effort_fields), never a key or URL
            from . import chatbridge
            f = chatbridge.settings(cfg, "standard", model, meta["effort"]).get("fields") or {}
            meta["parity"]["request"] = (json.dumps(f, sort_keys=True) if f else
                                         "no effort field (none in providers.chat.effort_fields): the server's default")
        durable_write(d / f"{sl}.json", json.dumps(meta))
    try:
        if via == BRIDGE_ROUTE:   # the logged-in CLI, no key: the agent's env points at the loopback server
            from .claudebridge import ClaudeCliBridge
            mk = bridge or (lambda m, e: ClaudeCliBridge.from_settings({"model": m, "effort": e}))
            make = lambda: mk(model, meta["effort"])   # noqa: E731
        elif via == CHAT_ROUTE:   # the chat endpoint's key stays in the relay, which also adds the effort's request
            from . import chatbridge   # fields (providers.chat.effort_fields), as for a chat worker at that effort
            mk = chat or (lambda m: chatbridge.Bridge.from_settings(
                chatbridge.settings(cfg, "standard", m, meta["effort"])))
            make = lambda: mk(model)   # noqa: E731
        else:
            make = None
        cmd = harborslice.COMMAND if via in (BRIDGE_ROUTE, CHAT_ROUTE) else ref_command(meta["effort"])

        def not_served() -> int:   # the endpoint does not serve the model: the row ends, not run (server)
            meta["not_run"] = NOT_SERVED
            durable_write(d / f"{sl}.json", json.dumps(meta))
            if run:
                write_table(run, s[REF]["note"])
            return ran
        if listed is None and trial is None and via == CHAT_ROUTE:
            listed = lambda m: _listed(mk(m))   # noqa: E731
        if listed is not None and listed(model) is False:
            return not_served()   # before any trial: no task call
        if meta.pop("not_run", None):   # served again: the row runs on
            durable_write(d / f"{sl}.json", json.dumps(meta))
        hist = serverhealth.History(folder() / HISTORY)
        done, put, dead, halt, info = _rows(d / f"{sl}.jsonl", meta["tasks"]), Lock(), Event(), Event(), {}
        gone = Event()   # a relay found the model missing mid-row: no more trials

        def attempt(t: str, n: int = 0) -> dict:
            if gate:   # a live stream on this host: the trial waits for it (occupancy.wait_free), lock freed
                gate(lock.pause) if lock else gate()
            if lock:   # held across trials; queues again after a stay-off freed it
                lock.take()
            b, e, st, cost = None, env, {}, None
            try:
                b = make() if make else None
                if via == CHAT_ROUTE and hasattr(b, "missing_max"):
                    b.missing_max = MISSING_MAX
                if b is not None:
                    b.start()
                    e = {**env, **b.openai_env()}
                got = (trial(model, t, d / "trials") if trial else
                       harborslice.trial(model, t, d / "trials", e, s, command=cmd, home=home))
                if b is not None:
                    st = b.stats() or {}
                    cost = b.cost_so_far() if hasattr(b, "cost_so_far") else None
                    info.setdefault("cli", (st.get("claude_cli") or {}).get("version", ""))
            finally:
                if b is not None:
                    b.stop()
                if lock:
                    lock.done()
            r = {"task": t, "at": clock(), "model": model, **got, **({"rerun": n} if n else {}),
                 **({"cost_usd": round(cost, 4)} if cost else {})}
            if b is not None:   # the relay's endpoint faults by kind and its capacity waits, on every row
                busy = st.get("busy") or {}
                r["relay"] = {"faults": dict(st.get("faults") or {}), "waits": int(busy.get("waits") or 0),
                              "gave_up": int(busy.get("gave_up") or 0)}
                stopped, scored = "timeout" if got.get("error") == "AgentTimeoutError" else None, not (got.get("reward") or 0) > 0
                with put:   # History is one file shared by the parallel trials
                    normal = hist.normal(model)
                    if serverhealth.judge(r, stopped, st, hist, scored=scored):
                        r["heldout_reward"], r["reward"] = got.get("reward"), None
                    elif scored and stopped and not normal:
                        # cut before the model's normal speed was known: judged again once it is (rejudge)
                        r["server_calls"] = {k: st[k] for k in ("busy", "upstream", "last_fault") if k in st}
                        r["server_calls"]["server"] = {"per_call": (st.get("server") or {}).get("per_call") or []}
                if r.get("outcome") != serverhealth.OUTCOME and (not (r.get("server") or {}).get("calls")
                                                                 or not_run(r)):
                    r.update(outcome=NOT_RUN, heldout_reward=r.get("reward"), reward=None)
            return r

        def rejudge() -> None:
            normal = hist.normal(model)
            for t in meta["tasks"]:
                r = done.get(t) or {}
                if not normal or "server_calls" not in r:
                    continue
                r = {k: v for k, v in r.items() if k != "server_calls"}
                why = serverhealth.classify("timeout" if r.get("error") == "AgentTimeoutError" else None,
                                            done[t]["server_calls"], normal)
                r["server_normal"] = normal
                if why:
                    serverhealth.mark(r, why)
                    r["heldout_reward"], r["reward"] = r.get("reward"), None
                keep(r)

        def keep(r: dict) -> None:
            with put:
                done[r["task"]] = r
                modelsuite._append(d / f"{sl}.jsonl", r)

        def one(t, n=0):
            bo.slot()   # no new trial while more than the back-off level run; running ones are never stopped
            try:
                if dead.is_set() or halt.is_set() or gone.is_set():
                    return   # a trial died, the model is gone, or the bench is held or stopped: start no more
                if stop and stop():
                    halt.set()
                    return
                if n:   # a not-run trial again: its old job is not resumed
                    shutil.rmtree(d / "trials" / sl / harborslice.slug(t), ignore_errors=True)
                r = attempt(t, n)
                keep(r)
                bo.seen(r)
                if ((r.get("relay") or {}).get("faults") or {}).get("model_missing"):
                    gone.set()
            except BaseException:
                dead.set()
                raise
            finally:
                bo.free()

        def pooled(todo: list, level: int) -> None:
            with ThreadPoolExecutor(max_workers=max(1, min(level, len(todo)))) as pool:
                for f in [pool.submit(one, t, int(done[t].get("rerun") or 0) + 1 if t in done else 0) for t in todo]:
                    f.result()
        ran = 0
        bo = benchbackoff.Backoff(model, rerun_server or s[REF]["parallel"], d / f"{sl}.backoff.jsonl", clock=clock)
        if rerun_server:   # the not-scored (server) rows again at the asked level, each new row replacing its old one
            todo = [t for t in meta["tasks"] if (done.get(t) or {}).get("outcome") == serverhealth.OUTCOME]
            if todo:
                pooled(todo, bo.level)
                if gone.is_set():
                    return not_served()
                if halt.is_set():
                    return ran
                ran += len(todo)
            bo.level = min(bo.level, s[REF]["parallel"]) if bo.backing else s[REF]["parallel"]
        while True:   # new trials, then not-run ones again (each up to reference.rounds times)
            todo = [t for t in meta["tasks"] if t not in done or (not_run(done[t])
                                                                   and int(done[t].get("rerun") or 0) < s[REF]["rounds"])]
            if not todo:
                break
            pooled(todo, bo.level)
            if gone.is_set():
                return not_served()
            if halt.is_set():
                return ran
            ran += len(todo)
        rejudge()
        while make is not None:   # the server's cut-offs: one at a time, each after a healthy probe
            left = [done[t] for t in meta["tasks"] if done[t].get("outcome") == serverhealth.OUTCOME
                    and int(done[t].get("rerun") or 0) < s[REF]["rounds"]]
            if not left:
                break
            ok, _ = serverhealth.wait_healthy(lambda: serverhealth.probe(make(), model), hist.normal(model),
                                              every_s=s[REF]["probe_every_s"], max_s=s[REF]["probe_max_s"],
                                              sleep=sleep, clock=clock)
            if not ok or (stop and stop()):
                break   # still slow (or held): the cut-offs stay not scored (server); a later start reruns them
            r = left[0]
            shutil.rmtree(d / "trials" / sl / harborslice.slug(r["task"]), ignore_errors=True)
            keep(attempt(r["task"], int(r.get("rerun") or 0) + 1))
            ran += 1
        if any(done[t].get("outcome") == serverhealth.OUTCOME and int(done[t].get("rerun") or 0) < s[REF]["rounds"]
               for t in meta["tasks"]):
            if run:
                write_table(run, s[REF]["note"])
            return ran   # reruns still owed: booked once the server lets them run
        extra = {}
        if make is not None:
            spent = sum(float(r.get("cost_usd") or 0) for r in _rows(d / f"{sl}.jsonl").values())
            extra = {"cost_usd": round(spent, 4)} if via == BRIDGE_ROUTE else {}
            if info.get("cli"):
                extra["claude_cli"] = info["cli"]
        sc = harborslice.score([done[t] for t in meta["tasks"] if not not_run(done[t])])
        modelsuite._append(ref_file(run), {
            "date": modelbench.day(clock()), "model": model, "suite": "terminalbench", "reference": True,
            "effort": meta["effort"], "agent": s["agent"], "dataset": s["dataset"], "started": meta["started"],
            "route": via, **parity(s, model, meta, via, harborslice.version(env)), "at": clock(), **extra, **sc,
            "score": sc["pass_rate"], **({"run": run} if run else {})})
        if run:
            write_table(run, s[REF]["note"])
        return ran
    finally:
        if lock:
            lock.release()
        held.close()


def _listed(bridge) -> bool | None:
    """Whether the chat endpoint's GET /models lists the bridge's model (None: unknown); never the key or URL."""
    try:
        return bridge.model_listed()
    except Exception:   # the check failing says nothing about the model
        return None


def not_run(r: dict) -> bool:
    """A trial that is not a run (user's rule): its agent made no model call (marked NOT_RUN when it ended), or
    failed calls broke it and it did not solve the task. Never scored, rerun up to reference.rounds times. A
    model that made calls and did not solve the task failed, the same for every model; a pass stands. A trial the
    harness broke before grading (harbor itself failed: a lost docker engine, say) has no grade, so it is not run
    either, never a fail."""
    from . import serverhealth
    srv = r.get("server") or {}
    if r.get("outcome") == NOT_RUN:
        return True
    if r.get("outcome") == serverhealth.OUTCOME:
        return False
    ungraded = r.get("reward") is None and str(r.get("error") or "").startswith("harbor rc ")
    return ungraded or (bool(srv.get("failed_calls")) and not (r.get("reward") or 0) > 0)


def _med(xs: list) -> float | None:
    import statistics
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 1) if xs else None


def _verdict(r: dict, lines: list[dict]) -> str | None:
    """A cut-off's verdict ("server" or "model", with why), None when the task was never cut."""
    from . import serverhealth
    cut = [x for x in lines if x.get("error") == "AgentTimeoutError" or x.get("outcome") == serverhealth.OUTCOME]
    if not cut:
        return None
    if r.get("outcome") == serverhealth.OUTCOME:
        return "server, not scored: " + "; ".join(r.get("server_reason") or [])
    if any(x.get("outcome") == serverhealth.OUTCOME for x in cut):
        first = next(x for x in cut if x.get("outcome") == serverhealth.OUTCOME)
        res = "passed" if (r.get("reward") or 0) > 0 else "cut at the limit at normal speed" if r.get(
            "error") == "AgentTimeoutError" else "failed"
        return f"server ({'; '.join(first.get('server_reason') or [])}); rerun on a healthy server: {res}"
    srv = r.get("server") or {}
    if r.get("server_normal"):
        n = r["server_normal"]
        return (f"model: cut at the limit while the server ran at normal speed (median wait {srv.get('wait_median_s')} s, "
                f"{srv.get('tps_median')} tokens/s; normal {n.get('wait_s')} s, {n.get('tps')} tokens/s)")
    if srv:
        return "model: cut at the limit; no normal server speed known for this model, so the server was not judged"
    return "model: cut at the limit (no server timing on this route)"


def table(run: str, note: str = "") -> str:
    """The named run's markdown table (terminalbench/<run>/table.md): passed of the task count per model over
    scored trials, cut-offs with a server-or-model verdict each, failed server calls (stream or parse errors
    included) of all calls over every trial run, median server wait and tokens/s, the limit,
    Claude dollars, parity and how each model was called. Read from the trial rows, so it shows a run in progress."""
    from . import benchparity, benchv2, harborslice, serverhealth
    d, metas = ref_dir(run), []
    for f in sorted(d.glob("*.json")):
        try:
            m = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and isinstance(m.get("tasks"), list):
            metas.append((f.stem, m))
    booked = {r["model"] for r in reference(run)}
    out = [f"# Terminal-Bench run {run}", "",
           "| model | effort | route | passed | 95% interval | scored | not scored (server) | faults | not run | cut-offs "
           "| failed calls | median server wait s | median tokens/s | Claude $ | state |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    cuts, pars = [], []
    for sl, m in metas:
        tasks, model = m["tasks"], m.get("model") or sl
        all_lines = list(_rows(d / f"{sl}.jsonl").values())
        latest = {t: r for t, r in _rows(d / f"{sl}.jsonl", tasks).items() if not not_run(r)}
        sc = harborslice.score(list(latest.values()))
        n_cut = 0
        for t in tasks:
            if t in latest and (v := _verdict(latest[t], [x for x in all_lines if x.get("task") == t])):
                n_cut += 1
                cuts.append(f"- {model} / {t}: {v}")
        srv = [x.get("server") or {} for x in latest.values() if x.get("outcome") != serverhealth.OUTCOME]
        cost = sum(float(x.get("cost_usd") or 0) for x in all_lines)
        calls = [(x.get("server") or {}) for x in all_lines]
        failed = (f"{sum(int(c.get('failed_calls') or 0) for c in calls)} of "
                  f"{sum(int(c.get('calls') or 0) for c in calls)}")
        lo, hi = benchv2.wilson(sc["passed"], len(tasks))   # over the task count, as the passed column
        par = {**(m.get("parity") or {}), "model": model, "effort": m.get("effort")}
        if par.get("route") in KIND:  # rows booked before a wording change print today's wording
            par["kind"] = KIND[par["route"]]
        pars.append(par)
        state = ("booked" if model in booked else f"not run ({m['not_run']})" if m.get("not_run")
                 else "running or owed reruns")
        out.append(f"| {model} | {m.get('effort')} | {m.get('route')} | {sc['passed']} of {len(tasks)} "
                   f"| {lo:.0%}-{hi:.0%} | {sc['trials']} "
                   f"| {sc.get('server', 0)} | {sc['faults']} | {len(tasks) - len(latest)} | {n_cut} | {failed} "
                   f"| {_med([x.get('wait_median_s') for x in srv])} | {_med([x.get('tps_median') for x in srv])} "
                   f"| {round(cost, 2) if m.get('route') == BRIDGE_ROUTE else '-'} "
                   f"| {state} |")
    lims = sorted({str(p.get("limit_s")) for p in pars})
    out += ["", "Passed counts scored trials only; the denominator is the task count. Not run: no trial yet, or "
            "one whose agent made no model call, whose failed server calls broke it short of a pass, or that harbor "
            "broke before grading. State not run (server: model not served): the chat endpoint's model list left the model "
            "out, before or during the row; nothing is scored or booked. Failed calls: server calls that failed (stream or parse errors included) of all "
            "calls, over every trial run, reruns included. 95% interval: the Wilson interval of passed of the task count; "
            "this is one run per model, so the bench cannot separate models whose intervals overlap. "
            f"Limit: {', '.join(lims) or '?'} "
            f"(seconds per task, or each task's own limit), counted as {pars[0].get('limit_counts') if pars else '?'}.", "",
            "## Cut-offs", ""] + (cuts or ["None."]) + [""]
    out += benchparity.section(pars, extra=("route", "request"))
    if note:
        out += ["## Note", "", note, ""]
    return "\n".join(out)


def write_table(run: str, note: str = "") -> Path:
    from .project import durable_write
    path = ref_dir(run) / "table.md"
    durable_write(path, table(run, note))
    return path


def launch(p, model: str, reference: bool = False) -> None:
    """Start `model`'s runner (or reference runner) detached (its own session), logging to terminalbench/<model>.log."""
    from .harborslice import slug
    folder().mkdir(parents=True, exist_ok=True)
    rt = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [rt, os.environ.get("PYTHONPATH")]))}
    with open(folder() / f"{(REF + '-') * reference}{slug(model)}.log", "ab") as out:
        argv = [sys.executable, "-m", "ttp", "model-bench", "terminalbench", *(["--reference"] * reference),
                "--runner", model]
        # an endpoint or chat reference runner takes reference.lock itself per trial (EndpointLock), so a stay-off
        # wait frees it; no `ttp lock` wraps the whole runner
        # own session: the paced benchmark runner outlives the daemon tick that starts it
        proc = subprocess.Popen(argv,
                                cwd=p.root, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
    schedjobs.record(p, proc.pid, REF_RUNNER if reference else RUNNER)   # found by `ttp detach --stop` and the daemon


def configured(p) -> bool:
    """Whether this project may have Terminal-Bench runners at all: model_bench.terminalbench is set, or a
    launch is recorded (so projects without the bench never scan for runners)."""
    mb = p.config().get("model_bench")
    return isinstance(mb, dict) and isinstance(mb.get("terminalbench"), dict) or bool(schedjobs.recorded(p))


def runner_sessions(p, live: list[dict] | None = None) -> list[dict]:
    """This project's live Terminal-Bench runners, the reference run aside: recorded by launch, or (started
    before the record) a session leader of this project (schedjobs.sessions) running `ttp model-bench
    terminalbench --runner` under the project's root in the caller's (the daemon's) cgroup. Each with `how`."""
    own = schedjobs._cgroup("self")
    out = []
    for s in schedjobs.sessions(p) if live is None else live:
        if rec := s.get("record"):
            if rec.get("what") == RUNNER:
                out.append({**s, "how": "recorded runner"})
        elif (s["words"][:2] == ["model-bench", "terminalbench"] and "--runner" in s["argv"]
              and "--reference" not in s["argv"] and s["env"].get("TTP_PROJECT") and schedjobs.under_root(p, s)
              and schedjobs._cgroup(s["sid"]) == own):
            out.append({**s, "how": "runs `ttp model-bench terminalbench --runner` here (started before its record)"})
    return out


def stop_runners(p, why: str, live: list[dict] | None = None) -> list[dict]:
    """End each runner_sessions() runner (schedjobs._end_session, audited) because `why`
    (modelbench.trial_stop): a runner that predates trial_stop never asks it."""
    return [r for s in runner_sessions(p, live)
            if (r := schedjobs._end_session(p, "terminalbench", s, why, reason=STOP_REASON, of=RUNNER))]
