# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Terminal-Bench cycles: the served chat models' paced, model-free Terminal-Bench runs
(`ttp model-bench terminalbench`), the ranking's first key (modelscore.TB) and each model's verdict.

A cycle is one pass of a model over the task list (harborslice.tasks). step() (the bench owner's
hourly command schedule benchowner.TB_SCHEDULE, or by hand) books every complete cycle and starts one
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
other model goes through the endpoint and its runner is wrapped in `ttp lock <reference.lock>` (default
LOCK, "" for none), so one endpoint model runs at a time. reference.parallel (default 1) runs that many of a
model's tasks at once. limit_s 0 (harborslice) gives every model its task's own limit.

Pre-flight (preflight): a cycle that cannot start (no working endpoint key, no harbor venv, no docker
compose) used to fail only in terminalbench.log. Each failure is counted in terminalbench/preflight.json and
a pass resets it; the PREFLIGHT_FAILS-th failure in a row raises one observation event (source
"model-bench", one per streak) naming the cause, so the owner fixes it the same day.
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

DIR, STATE, CYCLE_H, NEW_MODEL_H = "terminalbench", "state.json", 24.0, 3.0
REF, REF_FILE, REF_MODELS, REF_EFFORT = "reference", "reference.jsonl", ("claude-opus-5-5", "claude-sonnet-5-5"), "medium"
# Shown until a reference run is booked: an earlier run of the DEFAULT_TASKS subset (Terminus 2, 900 s per task).
SUBSET_REFERENCE = ({"model": "Opus 5.5", "effort": "low", "passed": 14, "trials": 20},
                    {"model": "Sonnet 5.5", "effort": "low", "passed": 14, "trials": 20})
LOCK, BRIDGE_ROUTE = "chat-server", "claude-cli"
PREFLIGHT_FILE, PREFLIGHT_FAILS = "preflight.json", 3
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
    par = r.get("parallel")
    ref = {"endpoint": str(r.get("endpoint") or ""),
           "models": [str(m) for m in ms if str(m).strip()] if isinstance(ms, list) else list(REF_MODELS),
           "effort": eff if eff.isalpha() else REF_EFFORT,   # it goes into the harbor command line
           "bridge": [str(m) for m in br if str(m).strip()] if isinstance(br, list) else [],
           "efforts": {str(k): str(v) for k, v in ef.items() if str(v).isalpha()} if isinstance(ef, dict) else {},
           "parallel": int(par) if isinstance(par, int) and not isinstance(par, bool) and par >= 1 else 1,
           "lock": str(r["lock"]) if isinstance(r.get("lock"), str) else LOCK}
    return {**harborslice.settings(cfg), "cycle_h": max(hours("cycle_h", CYCLE_H), 1.0),
            "new_model_h": hours("new_model_h", NEW_MODEL_H), REF: ref}


def preflight(p, cause: str = "", now: float | None = None) -> int:
    """Count a cycle's pre-flight: a failure (`cause`, one line) adds one, a pass (empty cause) resets the count.
    The PREFLIGHT_FAILS-th failure in a row queues one observation event naming the cause (one per streak).
    Returns the streak length."""
    from .project import durable_write
    now = time.time() if now is None else now
    f = folder() / PREFLIGHT_FILE
    try:
        st = json.loads(f.read_text())
        st = st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        st = {}
    if not cause:
        if st.get("fails"):
            durable_write(f, json.dumps({"fails": 0}))
        return 0
    st = {"fails": int(st.get("fails") or 0) + 1, "since": st.get("since") or now, "cause": cause[:400]}
    folder().mkdir(parents=True, exist_ok=True)
    durable_write(f, json.dumps(st))
    if st["fails"] >= PREFLIGHT_FAILS:
        fp = f"terminalbench-preflight:{int(st['since'])}"
        if not p.db.one("SELECT 1 FROM events WHERE fingerprint = ? AND kind = 'observation'", (fp,)):
            p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                   (now, "model-bench", "observation", fp, "normal",
                    f"Terminal-Bench cycles failed their pre-flight {st['fails']} times in a row, so no trial runs: "
                    f"{st['cause']}", "queued"))
    return st["fails"]


def folder() -> Path:
    from . import modelbench
    return modelbench.home() / DIR


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
           clock: Callable = time.time) -> int:
    """Run `model`'s cycle to its end, one paced trial at a time (module doc); returns trials run."""
    from . import harborslice, modelbench, modelsuite
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


def reference() -> list[dict]:
    """The booked reference rows, one per model (the first booked; module doc)."""
    out: dict[str, dict] = {}
    for r in _rows(folder() / REF_FILE).values():
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
    booked = {r["model"] for r in reference()}
    got: dict[str, list] = {"launched": [], "booked": [], "running": [], "not_served": []}
    for m in settings(cfg)[REF]["models"]:
        if m in booked:
            got["booked"].append(m)
        elif served is not None and m not in served and m not in settings(cfg)[REF]["bridge"]:
            got["not_served"].append(m)
        elif (held := _locked(folder() / REF / f"{slug(m)}.lock")) is None:
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


def ref_runner(cfg: dict, model: str, env: dict, *, trial: Callable | None = None, clock: Callable = time.time,
               bridge: Callable | None = None) -> int:
    """Run `model`'s reference tasks (resuming), then book its row once; returns trials run. A booked model
    runs nothing (module doc). A bridge model gets the claude CLI's loopback server (bridge(model, effort)
    builds it) for the run; reference.parallel tasks run at once."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from . import harborslice, modelbench, modelsuite
    from .project import durable_write
    if any(r["model"] == model for r in reference()):
        return 0
    d, sl = folder() / REF, harborslice.slug(model)
    held = _locked(d / f"{sl}.lock")
    if held is None:
        return 0   # its reference runner is already running
    s, home, ran = settings(cfg), modelbench.home(), 0
    via = BRIDGE_ROUTE if model in s[REF]["bridge"] else "endpoint"
    try:
        meta = json.loads((d / f"{sl}.json").read_text())
    except (OSError, ValueError):
        meta = {"started": clock(), "tasks": harborslice.tasks(home, s), "effort": ref_effort(s, model), "route": via}
        durable_write(d / f"{sl}.json", json.dumps(meta))
    srv, cost = None, 0.0
    try:
        if via == BRIDGE_ROUTE:   # the logged-in CLI, no key: the agent's env points at the loopback server
            from .claudebridge import ClaudeCliBridge
            srv = (bridge or (lambda m, e: ClaudeCliBridge.from_settings({"model": m, "effort": e})))(
                model, meta["effort"]).start()
            env = {**env, **srv.openai_env()}
        cmd = harborslice.COMMAND if via == BRIDGE_ROUTE else ref_command(meta["effort"])
        trial = trial or (lambda m, t, root: harborslice.trial(m, t, root, env, s, command=cmd, home=home))
        done, put = _rows(d / f"{sl}.jsonl", meta["tasks"]), Lock()

        dead = Event()

        def one(t):
            if dead.is_set():
                return   # a trial died: start no more
            try:
                r = {"task": t, "at": clock(), **trial(model, t, d / "trials")}
            except BaseException:
                dead.set()
                raise
            with put:
                done[t] = r
                modelsuite._append(d / f"{sl}.jsonl", r)
        todo = [t for t in meta["tasks"] if t not in done]
        with ThreadPoolExecutor(max_workers=max(1, min(s[REF]["parallel"], len(todo) or 1))) as pool:
            for f in [pool.submit(one, t) for t in todo]:
                f.result()
        ran = len(todo)
        extra = {}
        if srv is not None:
            cost = srv.cost_so_far()
            extra = {"cost_usd": round(cost, 4), "claude_cli": srv.stats().get("claude_cli", {}).get("version", "")}
        sc = harborslice.score([done[t] for t in meta["tasks"]])
        modelsuite._append(folder() / REF_FILE, {
            "date": modelbench.day(clock()), "model": model, "suite": "terminalbench", "reference": True,
            "effort": meta["effort"], "agent": s["agent"], "dataset": s["dataset"], "started": meta["started"],
            "route": via, "tasks": len(meta["tasks"]), "at": clock(), **extra, **sc, "score": sc["pass_rate"]})
        return ran
    finally:
        if srv is not None:
            srv.stop()
        held.close()


def launch(p, model: str, reference: bool = False) -> None:
    """Start `model`'s runner (or reference runner) detached (its own session), logging to terminalbench/<model>.log."""
    from .harborslice import slug
    folder().mkdir(parents=True, exist_ok=True)
    rt = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [rt, os.environ.get("PYTHONPATH")]))}
    with open(folder() / f"{(REF + '-') * reference}{slug(model)}.log", "ab") as out:
        argv = [sys.executable, "-m", "ttp", "model-bench", "terminalbench", *(["--reference"] * reference),
                "--runner", model]
        ref = settings(p.config())[REF]
        if reference and ref["lock"] and model not in ref["bridge"]:   # one endpoint model at a time
            argv = [sys.executable, "-m", "ttp", "lock", ref["lock"], "--", *argv]
        # own session: the paced benchmark runner outlives the daemon tick that starts it
        subprocess.Popen(argv,
                         cwd=p.root, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True)
