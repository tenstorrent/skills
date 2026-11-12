# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Harbor slice: Terminal-Bench 2.0 runs of the served chat models (`ttp model-bench terminalbench`), the
main input of the model ranking (modelscore.order) and of each model's verdict (modelsuite.verdict).

Setup (the one proven on the account's earlier full runs): Harbor's terminus-2 agent (setting agent) drives
each model as `openai/<model>` against the chat endpoint, whose URL and key reach only the child's
environment (OPENAI_BASE_URL, OPENAI_API_KEY), never its argv. One trial per task; each (model, task) is its
own Harbor job under <root>/<model>/<task>, so a restart resumes it (`harbor job resume`) and a finished one
is read, not rerun. The agent limit (limit_s, default LIMIT_S) becomes Harbor's agent timeout multiplier
where the task's own limit is known (a local dataset's task.toml), else the task's own limit holds. A
host-wide trial cap (slot) is sized to free cores (concurrency 0) or set. A trial the endpoint answered with
HTTP 429 is waited out ("retry after N s", else RETRY_S) and rerun, never scored; a setup error (an
exception before any verifier reward) reruns once, then counts as a fault, not a fail. A model's result
(score) is {passed, trials, pass_rate, median_min, faults}, booked as suite "terminalbench".

Task names: <modelbench.home()>/harbor-tasks.txt (setting tasks_file), one per line; none there: the public
DEFAULT_TASKS subset. Settings: model_bench.terminalbench {on, tasks_file, agent, limit_s, concurrency,
floor_share, dataset} (settings). The older suite slice (`ttp model-bench suite --harbor N`) uses the same
trial runner. Harbor is installed only into a venv the run owns (`python3 -m venv`, then that venv's pip
under cellenv.env), never with `pip install --user`; a failed install skips the run with its reason.
Tested with fakes only; parsing follows Harbor's trial result.json (verifier_result, exception_info).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

TASKS, PACKAGE = "harbor-tasks.txt", "harbor"   # TASKS under modelbench.home()
DATASET, AGENT, LIMIT_S, FLOOR_SHARE = "terminal-bench@2.0", "terminus-2", 900, 0.5
COMMAND = ("harbor run -y -q --dataset {dataset} -a {agent} -m openai/{model} -o {jobs} --job-name {name} -n 1 "
           "-i {task} --ak 'llm_call_kwargs={{\"num_retries\":8}}'")
RESUME = "harbor job resume -p {job}"
INSTALL_TIMEOUT_S, TASK_TIMEOUT_S, RETRY_S, CORES_PER_TRIAL, MAX_SLOTS, MAX_RATE_WAITS = 900, 3600, 60, 4, 8, 24
# A public 20-task subset of Terminal-Bench 2.0, used when the tasks file is missing or empty.
DEFAULT_TASKS = ("adaptive-rejection-sampler", "code-from-image", "count-dataset-tokens", "custom-memory-heap-crash",
                 "db-wal-recovery", "dna-insert", "financial-document-processor", "gcode-to-text", "git-multibranch",
                 "llm-inference-batching-scheduler", "model-extraction-relu-logits", "mteb-retrieve",
                 "password-recovery", "prove-plus-comm", "pytorch-model-cli", "pytorch-model-recovery",
                 "query-optimize", "rstan-to-pystan", "train-fasttext", "tune-mjcf")
_RATE = re.compile(r"\b429\b|rate.?limit", re.I)
_AFTER = re.compile(r"retry.?after\D{0,12}(\d+(?:\.\d+)?)", re.I)


def settings(cfg: dict) -> dict:
    """model_bench.terminalbench with defaults (module doc)."""
    mb = cfg.get("model_bench") if isinstance(cfg.get("model_bench"), dict) else {}
    s = mb.get("terminalbench") if isinstance(mb.get("terminalbench"), dict) else {}

    def num(k, d, lo):
        v = s.get(k, d)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= lo else float(d)
    return {"on": s.get("on", True) is not False, "tasks_file": str(s.get("tasks_file") or TASKS),
            "agent": str(s.get("agent") or AGENT), "limit_s": num("limit_s", LIMIT_S, 60),
            "concurrency": int(num("concurrency", 0, 0)), "floor_share": num("floor_share", FLOOR_SHARE, 0),
            "dataset": str(s.get("dataset") or DATASET)}


def tasks(home: Path, st: dict) -> list[str]:
    f = home / st["tasks_file"]
    names = [x.strip() for x in f.read_text().splitlines() if x.strip() and not x.startswith("#")] if f.is_file() else []
    return names or list(DEFAULT_TASKS)


def slug(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in s)


def cap(st: dict) -> int:
    """The host-wide trial cap: the setting, else free cores / CORES_PER_TRIAL (1..MAX_SLOTS)."""
    if st.get("concurrency"):
        return int(st["concurrency"])
    free = (os.cpu_count() or 1) - os.getloadavg()[0]
    return max(1, min(MAX_SLOTS, int(free // CORES_PER_TRIAL)))


def slot(home: Path, st: dict, sleep: Callable = time.sleep):
    """Hold one of the host's trial slots (an flock'd file); returns its open file, closed to release."""
    d = home / "tb-slots"
    d.mkdir(parents=True, exist_ok=True)
    while True:
        for i in range(cap(st)):
            f = open(d / f"{i}.lock", "w")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except OSError:
                f.close()
        sleep(30)


def _run(cmd: list[str], env: dict, timeout: float) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"
    return r.returncode, (r.stderr or r.stdout or "")[-500:]


def install(root: Path, runner: Callable = _run) -> tuple[Path | None, str]:
    """(the venv holding PACKAGE, '') or (None, why). Builds root/venv once; a failure is never retried."""
    from . import cellenv
    from .project import durable_write
    venv = root / "venv"
    if (venv / ".installed").exists():
        return venv, ""
    if (root / "install-failed").exists():
        return None, (root / "install-failed").read_text()
    env = cellenv.env(venv, dict(os.environ))
    for cmd in ([sys.executable, "-m", "venv", str(venv)],
                [str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet", PACKAGE]):
        rc, out = runner(cmd, env, INSTALL_TIMEOUT_S)
        if rc:
            why = f"harbor install failed (no other route is tried): {out.strip()[-300:]}"
            root.mkdir(parents=True, exist_ok=True)
            durable_write(root / "install-failed", why)
            return None, why
    venv.mkdir(parents=True, exist_ok=True)
    durable_write(venv / ".installed", "")
    return venv, ""


def reward(jobs: Path) -> list[float]:
    """Each trial's verifier reward under `jobs` (trial result.json files)."""
    out = []
    for f in sorted(jobs.rglob("result.json")):
        try:
            v = (json.loads(f.read_text()) or {}).get("verifier_result") or {}
            r = (v.get("rewards") or {}).get("reward", v.get("reward"))
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(r, (int, float)):
            out.append(float(r))
    return out




def _stamp(v) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def outcome(job: Path) -> dict | None:
    """A finished job's trial: {"reward", "min", "error", "rate_s"?}; None while unfinished (no finished_at)."""
    try:
        if not json.loads((job / "result.json").read_text()).get("finished_at"):
            return None
    except (OSError, ValueError, AttributeError):
        return None
    best: dict = {"reward": None, "min": None, "error": "no trial result"}
    for f in sorted(job.glob("*/result.json")):
        try:
            t = json.loads(f.read_text()) or {}
        except (OSError, ValueError):
            continue
        v = t.get("verifier_result") or {}
        r = (v.get("rewards") or {}).get("reward", v.get("reward")) if isinstance(v, dict) else None
        exc = t.get("exception_info") or {}
        kind = str(exc.get("exception_type") or "")
        a, b = _stamp(t.get("started_at")), _stamp(t.get("finished_at"))
        got = {"reward": float(r) if isinstance(r, (int, float)) else 0.0 if kind == "AgentTimeoutError" else None,
               "min": round((b - a) / 60, 2) if a and b else None, "error": kind or None}
        text = f"{kind} {exc.get('exception_message') or ''}"
        if got["reward"] is None and _RATE.search(text):
            m = _AFTER.search(text)
            got["rate_s"] = float(m.group(1)) if m else RETRY_S
        if best["reward"] is None or (got["reward"] or 0) > (best["reward"] or 0):
            best = got
    return best


def _task_limit(dataset: str, task: str) -> float | None:
    """The task's own agent limit (s) from a local dataset's task.toml; None for a registry dataset."""
    f = Path(dataset) / task / "task.toml"
    if not f.is_file():
        return None
    m = re.search(r"\[agent\][^\[]*?timeout_sec\s*=\s*([0-9.]+)", f.read_text(), re.S)
    return float(m.group(1)) if m else None


def trial(model: str, task: str, root: Path, env: dict, st: dict, runner: Callable = _run,
          sleep: Callable = time.sleep, command: str | None = None, home: Path | None = None) -> dict:
    """One (model, task) trial under root/<model>/<task>, resumed or read when already there (module doc)."""
    job, reruns, waits = root / slug(model) / slug(task), 1, MAX_RATE_WAITS
    while True:
        if waits <= 0:
            return {"reward": None, "min": None, "error": "endpoint rate limit (429) did not clear"}
        got = outcome(job)
        if got and got["reward"] is not None:
            return got
        if got and "rate_s" in got:   # the endpoint's 429: wait it out and rerun; never scored
            shutil.rmtree(job, ignore_errors=True)
            sleep(min(got["rate_s"], TASK_TIMEOUT_S))
            waits -= 1
            continue
        if got:   # a setup error: rerun once, then a fault
            if not reruns:
                return got
            reruns -= 1
            shutil.rmtree(job, ignore_errors=True)
        if (job / "config.json").exists():
            cmd = [w.format(job=job) for w in shlex.split(RESUME)]
        else:
            job.parent.mkdir(parents=True, exist_ok=True)
            cmd = [w.format(model=model, task=task, jobs=job.parent, name=job.name, dataset=st["dataset"],
                            agent=st["agent"]) for w in shlex.split(command or COMMAND)]
            if lim := _task_limit(st["dataset"], task):
                cmd += ["--agent-timeout-multiplier", f"{st['limit_s'] / lim:.4g}"]
        lock = slot(home or root, st, sleep)
        try:
            rc, out = runner(["nice", "-n", "10", *cmd], env, st["limit_s"] * 4 + TASK_TIMEOUT_S)
        finally:
            lock.close()
        if outcome(job) is None:   # harbor ended without finishing the job
            if _RATE.search(out):
                m = _AFTER.search(out)
                sleep(min(float(m.group(1)) if m else RETRY_S, TASK_TIMEOUT_S))
                waits -= 1
                continue
            if not reruns:
                return {"reward": None, "min": None, "error": f"harbor rc {rc}: {out.strip()[-200:]}"}
            reruns -= 1


def score(got: list[dict]) -> dict:
    """{"passed", "trials", "pass_rate", "median_min", "faults"} of a model's trials; faults are not trials."""
    done = [g for g in got if g["reward"] is not None]
    mins = [g["min"] for g in done if g["min"] is not None]
    passed = sum(g["reward"] > 0 for g in done)
    return {"passed": passed, "trials": len(done), "pass_rate": round(passed / len(done), 3) if done else None,
            "median_min": round(statistics.median(mins), 1) if mins else None, "faults": len(got) - len(done)}


def child_env(venv: Path, url: str = "", key: str = "") -> dict:
    """The child's environment: the venv's, plus the endpoint's URL and key (never argv)."""
    from . import cellenv
    env = cellenv.env(venv, dict(os.environ))
    if url:
        env.update(OPENAI_BASE_URL=url, OPENAI_API_KEY=key)
    return env


def row(model: str, date: str, n: int, d: Path, out: Path, opts: dict, runner: Callable = _run) -> dict:
    """The suite slice (--harbor N): N tasks drawn by the date; the score plus {"cells", "errors", "wall_s"}."""
    from . import modelsuite
    st = {**settings({}), **(opts.get("terminalbench") or {})}
    venv, why = install(out / "harbor", runner)
    if not venv:
        return {"pass_rate": None, "cells": 0, "skipped": why}
    t0, env = time.monotonic(), child_env(venv, opts.get("endpoint_url") or "", opts.get("endpoint_key") or "")
    got = score([trial(model, t, out / "harbor", env, st, runner, home=d, command=opts.get("harbor_cmd"))
                 for t in modelsuite.draw(tasks(d, st), n, date + "/harbor")])
    return {**got, "cells": got["trials"], "errors": got["faults"], "wall_s": round(time.monotonic() - t0, 1)}
