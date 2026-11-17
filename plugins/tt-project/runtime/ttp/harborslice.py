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

Task names: <modelbench.home()>/harbor-tasks.txt (setting tasks_file), one per line, an optional override;
none there: the full set of a local dataset folder (setting dataset, each task a folder with task.toml),
the DEFAULT_TASKS subset first so a partial cycle still compares with earlier subset runs; a registry
dataset name (no folder): the DEFAULT_TASKS subset only. Settings: model_bench.terminalbench {on, tasks_file, agent, limit_s, concurrency,
floor_share, dataset, compose} (settings; limit_s 0 keeps each task's own limit, the same for every model). Before any trial, compose() makes sure `docker compose` runs, linking
a compose plugin found in docker's usual cli-plugins folders or the setting compose into a docker config folder
the bench owns; none: the run stops with one message naming what is missing. The older suite slice (`ttp model-bench suite --harbor N`) uses the same
trial runner. Harbor is installed only into a venv the run owns, never with `pip install --user`: built on a
python3.x on PATH of at least MIN_PY (Harbor needs 3.12), else with `uv venv --python 3.12` (uv's interpreters
and cache under the bench folder, never ~/.local), then that venv's pip under cellenv.env. A failed install
skips the run with its reason and is retried after INSTALL_RETRY_S or once the install route changes.
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
# Where docker looks for CLI plugins (compose is docker-compose there): the user's folder under the home, then
# the system's usual ones.
USER_PLUGIN_DIR = "." + "docker/cli-plugins"
PLUGIN_DIRS = ("/usr/local/lib/docker/cli-plugins", "/usr/local/libexec/docker/cli-plugins",
               "/usr/lib/docker/cli-plugins", "/usr/libexec/docker/cli-plugins")
# Harbor needs Python 3.12+; RECIPE names this install route, so a failure marker of an older one is stale.
MIN_PY, RECIPE, INSTALL_RETRY_S = (3, 12), "venv-py3.12+", 6 * 3600
INSTALL_TIMEOUT_S, TASK_TIMEOUT_S, RETRY_S, CORES_PER_TRIAL, MAX_SLOTS, MAX_RATE_WAITS = 900, 3600, 60, 4, 8, 24
# A public 20-task subset of Terminal-Bench 2.0: run first in each cycle; alone without a local dataset folder.
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
            "agent": str(s.get("agent") or AGENT),
            "limit_s": 0.0 if s.get("limit_s") == 0 and not isinstance(s.get("limit_s"), bool) else num("limit_s", LIMIT_S, 60),
            "concurrency": int(num("concurrency", 0, 0)), "floor_share": num("floor_share", FLOOR_SHARE, 0),
            "dataset": str(s.get("dataset") or DATASET), "compose": str(s.get("compose") or "")}


def tasks(home: Path, st: dict) -> list[str]:
    f = home / st["tasks_file"]
    names = [x.strip() for x in f.read_text().splitlines() if x.strip() and not x.startswith("#")] if f.is_file() else []
    if names:
        return names
    d = Path(st["dataset"]).expanduser()
    full = sorted(t.parent.name for t in d.glob("*/task.toml")) if d.is_dir() else []
    first = [t for t in DEFAULT_TASKS if t in full] if full else list(DEFAULT_TASKS)
    return first + [t for t in full if t not in first]


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


def _version(py: str) -> tuple[int, int] | None:
    """`py`'s (major, minor), None when it does not run."""
    try:
        r = subprocess.run([py, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=30)
        return tuple(int(x) for x in r.stdout.split(".")) if r.returncode == 0 else None   # type: ignore[return-value]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def interpreter(which: Callable = shutil.which, version: Callable = _version) -> str | None:
    """A python3 on PATH new enough for Harbor (MIN_PY), newest name first; None when there is none."""
    seen = set()
    for py in (sys.executable, *(which(f"python3.{n}") for n in range(20, MIN_PY[1] - 1, -1)), which("python3")):
        if py and py not in seen:
            seen.add(py)
            v = version(py)
            if v and v >= MIN_PY:
                return py
    return None


def uv(which: Callable = shutil.which) -> str | None:
    """uv on PATH or in its own installer's usual folders (a daemon's PATH may lack them)."""
    home = Path.home()
    return which("uv") or next((str(p) for p in (home / ".local" / "bin" / "uv", home / ".cargo" / "bin" / "uv")
                                if p.is_file() and os.access(p, os.X_OK)), None)


def install(root: Path, runner: Callable = _run, which: Callable = shutil.which,
            version: Callable = _version) -> tuple[Path | None, str]:
    """(the venv holding PACKAGE, '') or (None, why). Builds root/venv on a python3 on PATH of at least MIN_PY,
    else with `uv venv --python MIN_PY` (uv's interpreters and cache under root, never ~/.local), else fails
    naming the version needed. A failed install is not retried for INSTALL_RETRY_S; a failure marker of an
    earlier recipe (or older) is stale and tried again."""
    from . import cellenv
    from .project import durable_write
    venv, failed = root / "venv", root / "install-failed"
    if (venv / ".installed").exists():
        return venv, ""
    try:
        mark = json.loads(failed.read_text())
        if mark.get("recipe") == RECIPE and time.time() - float(mark.get("ts") or 0) < INSTALL_RETRY_S:
            return None, str(mark.get("why") or "harbor install failed")
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    failed.unlink(missing_ok=True)

    def fail(why: str) -> tuple[None, str]:
        root.mkdir(parents=True, exist_ok=True)
        durable_write(failed, json.dumps({"recipe": RECIPE, "ts": time.time(), "why": why}))
        return None, why
    need = "%d.%d" % MIN_PY
    env = cellenv.env(venv, dict(os.environ))
    py = interpreter(which, version)
    if py:
        make = [py, "-m", "venv", str(venv)]
    elif u := uv(which):
        make = [u, "venv", "--seed", "--python", need, str(venv)]
        env = {**env, "UV_PYTHON_INSTALL_DIR": str(root / "uv-python"), "UV_CACHE_DIR": str(root / "uv-cache")}
    else:
        return fail(f"harbor needs Python {need} or newer: no python3.x of at least {need} on PATH and no uv "
                    f"to fetch one (install a python{need}, or uv, for this user)")
    if venv.exists():   # a venv of a failed try, maybe on an older Python: rebuilt from scratch
        shutil.rmtree(venv)
    for cmd in (make, [str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet", PACKAGE]):
        rc, out = runner(cmd, env, INSTALL_TIMEOUT_S)
        if rc:
            return fail(f"harbor install failed ({Path(cmd[0]).name} {cmd[1]}): {out.strip()[-300:]}")
    venv.mkdir(parents=True, exist_ok=True)
    durable_write(venv / ".installed", RECIPE)
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
            if Path(st["dataset"]).expanduser().is_dir():   # a local dataset folder: harbor takes it by --path
                cmd = ["--path" if w == "--dataset" else str(Path(w).expanduser()) if w == st["dataset"] else w
                       for w in cmd]
            lim = _task_limit(st["dataset"], task)
            if lim and st["limit_s"]:
                cmd += ["--agent-timeout-multiplier", f"{st['limit_s'] / lim:.4g}"]
        lock = slot(home or root, st, sleep)
        try:
            rc, out = runner(["nice", "-n", "10", *cmd], env, (st["limit_s"] or lim or LIMIT_S) * 4 + TASK_TIMEOUT_S)
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
    """{"passed", "trials", "pass_rate", "median_min", "faults"} of a model's trials; faults are not trials.
    A trial the server cut (outcome "server", serverhealth) is neither: it is counted as "server", not scored."""
    srv = sum(g.get("outcome") == "server" for g in got)
    done = [g for g in got if g["reward"] is not None and g.get("outcome") != "server"]
    mins = [g["min"] for g in done if g["min"] is not None]
    passed = sum(g["reward"] > 0 for g in done)
    return {"passed": passed, "trials": len(done), "pass_rate": round(passed / len(done), 3) if done else None,
            "median_min": round(statistics.median(mins), 1) if mins else None,
            "faults": len(got) - len(done) - srv, **({"server": srv} if srv else {})}


def version(env: dict) -> str:
    """The harbor version installed in the child's venv (its dist-info), '' when unknown."""
    v = (env or {}).get("VIRTUAL_ENV")
    for f in sorted(Path(v).glob("lib/*/site-packages/harbor-*.dist-info")) if v else []:
        return f.name[len("harbor-"):-len(".dist-info")]
    return ""


def compose(root: Path, st: dict, env: dict | None = None, runner: Callable = _run,
            which: Callable | None = None) -> tuple[dict | None, str]:
    """({extra env}, '') once `docker compose` runs, else (None, why naming what is missing). Harbor starts
    each task's container with it. Docker's own lookup first; else a compose plugin from the setting compose
    (the plugin file, or a folder holding docker-compose) or docker's usual cli-plugins folders
    (PLUGIN_DIRS, the user's own first), linked into a docker config folder the bench owns
    (root/docker-config) and named by DOCKER_CONFIG. Nothing is installed or copied: only the plugin link
    is there, so no docker settings or credentials move."""
    env = dict(os.environ if env is None else env)
    docker = (which or shutil.which)("docker")
    if not docker:
        return None, "docker is missing: no docker on PATH, so no Terminal-Bench trial can start"
    if runner([docker, "compose", "version"], env, 60)[0] == 0:
        return {}, ""
    own = root / "docker-config"
    link = own / "cli-plugins" / "docker-compose"
    tried = [Path(st["compose"]).expanduser()] if st.get("compose") else []
    tried += [root / "compose"]   # the bench's own folder: a compose binary put there needs no setting
    tried += [Path(env["DOCKER_CONFIG"]) / "cli-plugins"] if env.get("DOCKER_CONFIG") else []
    tried += [Path.home() / USER_PLUGIN_DIR, *map(Path, PLUGIN_DIRS)]
    for c in tried:
        f = c / "docker-compose" if c.is_dir() else c
        if not (f.is_file() and os.access(f, os.X_OK)) or f.parent.resolve() == link.parent.resolve():
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(f.resolve())
        extra = {"DOCKER_CONFIG": str(own)}
        if runner([docker, "compose", "version"], {**env, **extra}, 60)[0] == 0:
            return extra, ""
    return None, ("docker compose is missing: `docker compose version` fails and no working docker-compose plugin "
                  "is in " + ", ".join(str(c) for c in tried) + "; put the compose plugin in one of those folders "
                  "or set model_bench.terminalbench.compose to its path")


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
