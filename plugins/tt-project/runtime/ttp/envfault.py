# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A check runner the host lacks: an environment fault, not a failed check.

Before check commands run (`ttp checks`, `ttp push`, the push queue's batch), precheck() looks at
each one's first runner, when it is the shared check path's: the python of its first simple command
must be on PATH (or an existing absolute path), a `python -m <module>` there (a bare pytest included,
after push.venv_cmd) must find that module with that python, and a bare pytest left as written must
be on PATH. One that cannot is a fault with the signature env:missing-runner:<name>; a pytest older
than the repository's pytest `minversion` is env:old-runner:pytest. Any other program (a task's own
extra command, typo or not) is the command's business: it runs and fails as it would.
A fault counts against no change or branch: `ttp checks` and `ttp push` exit 75 (wait) and print a
retry_when (`ttp checks --env-ok <sig>`, its own fault only), and the push queue requeues the batch and holds until the
runner is back, on a fault its own batch found only. Each fault is recorded once in the project's
state, with one coordinator observation per fault however many tasks hit it; it clears once its
command, run again where it was found, no longer hits it, or once that checkout is gone.

A check with a host (push.remote_host, remotecheck) needs no local runner: its fault is the host,
env:check-host:<host>, when it is paused (remotecheck.paused: probed with no ssh), ssh cannot reach
it or it lacks git or tar (probed before the checks, or the check exits remotecheck.FAULT_RC mid-run).
It clears once the pause lifts and ssh reaches the host again.

A missing or old runner's observation also names its likely cause (cause), read-only and model-free:
the pip runs of the CAUSE_WINDOW_S before it (pip's selfcheck cache: one file per interpreter prefix,
stamped at each run), and the tt-project detached jobs of the account's projects that ran then and
whose logs name one of those prefixes."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

KV = "env_faults"
PREFIX = "env:missing-runner:"
OLD = "env:old-runner:"
PROBE_S = 60            # the push queue's recheck of open faults: at most once per this
PROBE_TIMEOUT_S = 30
HOLD_ALERT_S = 6 * 3600   # a queue hold this old raises one more observation
CAUSE_WINDOW_S = 600      # pip runs and detached jobs this long before a runner fault are its likely cause
CAUSE_MAX = 5             # pip runs and jobs named, each
_LOG_SCAN = 1 << 20       # bytes read from each end of a job's log
_PY = re.compile(r"python[0-9.]*$")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_OPS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&", "\n"}
_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}", "time"}
_SKIP = {"for", "case", "function", "select", "[[", "[", "test"}   # the segment is no plain program run
_WRAPPERS = {"nice", "nohup"}
_TEST_RUNNERS = {"pytest", "py.test"}
# One line (a stand-in python that logs "$@" logs one line); with a 2nd argument it prints __version__.
_FIND = ("import sys, importlib.util as u; u.find_spec(sys.argv[1]) or sys.exit(3); "
         "len(sys.argv) > 2 and print(getattr(__import__(sys.argv[1]), '__version__', ''))")
_MINVERSION = re.compile(r"""^\s*minversion\s*[=:]\s*["']?([0-9][0-9A-Za-z.]*)""", re.M)


class Fault(Exception):
    """Raised by a check runner that found a fault (the push queue's batch); `.fault` is its dict."""

    def __init__(self, fault: dict):
        super().__init__(line(fault))
        self.fault = fault


def _segments(cmd: str) -> list[list[str]]:
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    segs, cur = [], []
    for tok in lex:
        if tok in _OPS or set(tok) <= set(";&|()"):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)
    return segs


def _program(words: list[str]) -> tuple[str, list[str]] | None:
    """(program, its arguments) of one simple command, past assignments, keywords and wrappers
    (`env`, `timeout <duration>`, `nice`, ...); None when it is no plain program run."""
    i = 0
    while i < len(words):
        w = words[i]
        if _ASSIGN.match(w) or w in _KEYWORDS:
            i += 1
        elif w == "env":
            i += 1
            while i < len(words) and (words[i].startswith("-") or _ASSIGN.match(words[i])):
                i += 2 if words[i] in ("-u", "-C", "--unset", "--chdir") else 1   # those take a value
        elif w == "timeout":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-s", "-k", "--signal", "--kill-after") else 1
            i += 1   # the duration
        elif w in _WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] == "-n" else 1
        else:
            break
    if i >= len(words) or words[i] in _SKIP or any(c in words[i] for c in "$`*?"):
        return None
    return words[i], words[i + 1:]


def _module(args: list[str]) -> str | None:
    """The module of a `python [options] -m <module>` run, else None."""
    for j, w in enumerate(args):
        if w == "-m" and j + 1 < len(args):
            return args[j + 1]
        if w.startswith("-m") and len(w) > 2:
            return w[2:]
        if not w.startswith("-") or w in ("-c", "-"):
            return None
    return None


def _resolve(prog: str, path: str) -> str | None:
    if "/" in prog:
        return prog if os.access(prog, os.X_OK) else None
    return shutil.which(prog, path=path)


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def minversion(top: str | Path) -> str:
    """The pytest `minversion` of the config file pytest reads in `top` (pytest.ini, .pytest.ini,
    pyproject.toml [tool.pytest.ini_options], tox.ini [pytest], setup.cfg [tool:pytest]), or ""."""
    for name, section in (("pytest.ini", "pytest"), (".pytest.ini", "pytest"),
                          ("pyproject.toml", "tool.pytest.ini_options"), ("tox.ini", "pytest"),
                          ("setup.cfg", "tool:pytest")):
        try:
            text = (Path(top) / name).read_text(errors="replace")
        except OSError:
            continue
        body = re.split(r"^\s*\[" + re.escape(section) + r"\]\s*$", text, maxsplit=1, flags=re.M)
        if len(body) < 2:
            continue   # pytest reads the next file only when this one has no pytest section
        m = _MINVERSION.search(re.split(r"^\s*\[", body[1], maxsplit=1, flags=re.M)[0])
        return m.group(1) if m else ""
    return ""


def runners(cmd: str, top: str | Path, env: dict | None = None) -> list[dict]:
    """The runner `cmd` needs first as it would run in `top` (push.venv_cmd applied), when it is the
    shared check path's: the python of its first simple command that runs one (and its
    `python -m <module>` when that module is not the repository's own), or a bare pytest. Any other
    first program is none: the command runs and says what is wrong. Only the first: a later command
    may run what an earlier one installs. None after a step that may change PATH (source, export,
    eval, a PATH= assignment)."""
    from . import push
    env = dict(os.environ if env is None else env)
    path = env.get("PATH") or os.defpath
    got = _first_run(cmd, top, env)
    if not got:
        return []
    prog, args = got
    if "/" in prog and not os.path.isabs(prog):
        return []   # a script of the repository's: the change's business
    python = bool(_PY.search(Path(prog).name))
    if not python and Path(prog).name not in _TEST_RUNNERS:
        return []
    mod = _module(args) if python else None
    if mod and not push._in_repo(mod.split(".")[0], top):
        mod = mod.split(".")[0]
        low = minversion(top) if mod == "pytest" else ""
        return [{"kind": "module", "python": prog, "module": mod, "path": path, "cmd": cmd,
                 "cwd": str(top), **({"min": low} if low else {})}]
    return [{"kind": "program", "name": prog, "path": path, "cmd": cmd, "cwd": str(top)}]


def _first_run(cmd: str, top: str | Path | None, env: dict) -> tuple[str, list[str]] | None:
    """(program, arguments) of the first program `cmd` runs (push.venv_cmd applied in `top`; None:
    as written), past builtins, assignments and wrappers (_program); None when that is not knowable
    up front (it does not parse, a step may change PATH, a conditional or loop decides it)."""
    from . import push
    try:
        segs = _segments(push.venv_cmd(cmd, top, env) if top is not None else cmd)
    except ValueError:
        return None   # does not parse: the run says what is wrong
    for words in segs:
        if any(w.startswith("PATH=") for w in words[:1]) or words[:1] in (["source"], ["."], ["export"],
                                                                          ["eval"], ["exec"]):
            return None
        if words[0] in _SKIP or words[0] in _KEYWORDS - {"time", "!"}:
            return None   # a conditional or loop: what runs depends on it
        got = _program(words)
        if not got:
            continue
        if any(w.startswith("PATH=") for w in words if _ASSIGN.match(w)):
            return None
        if "/" not in got[0] and got[0] in push.BUILTINS:
            continue
        return got
    return None


def missing_program(cmd: str, top: str | Path, env: dict | None = None, host: str = "",
                    host_env: dict | None = None) -> str:
    """The program check command `cmd` runs first, when `command -v` does not find it where the
    check runs ("" when it does or cannot tell): with `env` in `top` here, or, with a check `host`,
    there over ssh with the check's `host_env` exported, as remotecheck runs it. Only a program
    precheck leaves to the command (a python or a test runner is its runner fault, a script of the
    repository's the change's), and only the first: a later command may run what an earlier one
    installs. ssh failing is no answer here (precheck probes the host)."""
    env = dict(os.environ if env is None else env)
    got = _first_run(cmd, None, env)   # push.venv_cmd changes only pytest runs, which are not looked at here
    if not got:
        return ""
    prog = got[0]
    if ("/" in prog and not os.path.isabs(prog)) or _PY.search(Path(prog).name) or Path(prog).name in _TEST_RUNNERS:
        return ""
    if host:
        from . import remotecheck
        argv = remotecheck._ssh(host, remotecheck.exports(host_env or {})
                                + f"command -v {shlex.quote(prog)} >/dev/null 2>&1 || exit 3")
        cwd = None
    else:
        argv, cwd = ["sh", "-c", 'command -v "$1" >/dev/null 2>&1 || exit 3', "sh", prog], str(top)
    try:
        r = subprocess.run(argv, cwd=cwd, env=None if host else env, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return ""
    return prog if r.returncode == 3 else ""


def probe(r: dict, env: dict | None = None) -> dict | None:
    """The fault of one runner (runners()), or None when it is there. A probe that cannot tell (a
    timeout, an interpreter that crashes) is no fault: the check runs and says what it says."""
    if r.get("kind") == "host":
        from . import remotecheck
        host = str(r.get("host") or "")
        why = remotecheck.paused(host, r.get("cwd") or None) or remotecheck.reachable(host)
        return {**r, "why": why} if why else None
    path = str(r.get("path") or os.defpath)
    if r.get("kind") == "program":
        name = str(r.get("name") or "")
        if _resolve(name, path):
            return None
        return {**r, "sig": PREFIX + Path(name).name, "runner": Path(name).name, "python": ""}
    py = _resolve(str(r.get("python") or ""), path)
    if not py:
        name = Path(str(r.get("python") or "python")).name
        return {**r, "sig": PREFIX + name, "runner": name, "python": str(r.get("python") or "")}
    run_env = {**(os.environ if env is None else env), "PATH": path}
    cwd = r.get("cwd") if r.get("cwd") and Path(str(r["cwd"])).is_dir() else None
    low = str(r.get("min") or "")
    try:
        got = subprocess.run([py, "-c", _FIND, str(r["module"]), *(["v"] if low else [])], cwd=cwd, env=run_env,
                             capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if got.returncode == 3:
        return {**r, "sig": PREFIX + str(r["module"]), "runner": str(r["module"]), "python": py}
    have = got.stdout.strip().splitlines()[-1:] if got.returncode == 0 and low else []
    if have and _version(have[0]) and _version(have[0]) < _version(low):
        return {**r, "sig": OLD + str(r["module"]), "runner": str(r["module"]), "python": py, "version": have[0]}
    return None


def precheck(cmds: list, top: str | Path, env: dict | None = None) -> dict | None:
    """The first runner fault of the check commands `cmds` run in `top` with `env`, or None. A check
    with a host is probed there (reachable), once per host."""
    from . import push, remotecheck
    seen = set()
    for c in cmds:
        if host := push.remote_host(c):
            if host not in seen:
                seen.add(host)
                if f := probe(remotecheck.fault(host, str(c), str(top)), env):
                    return f
            continue
        for r in runners(str(c), top, env):
            if f := probe(r, env):
                return f
    return None


def line(f: dict) -> str:
    what = (f"the checks' pinned Python env cannot be made from {f.get('lock')}: {f.get('why')}"
            if f.get("kind") == "pyenv" else
            f"{f['python']} has {f['runner']} {f['version']}, below the repository's minversion {f.get('min')}"
            if f.get("version") else
            f"{f['python']} cannot import {f['runner']}" if f.get("kind") == "module" and f.get("python")
            else f"check host {f.get('host')} is {f['why']}" if f.get("kind") == "host"
            and str(f.get("why") or "").startswith("paused by ")
            else f"check host {f.get('host')} cannot run checks over ssh"
            + (f" ({f['why']})" if f.get("why") else "") if f.get("kind") == "host"
            else f"{f.get('runner')} is not on PATH or not executable")
    return (f"environment fault {f.get('sig')}: {what} (needed by {f.get('cmd')!r}). This is the host, "
            "not the change: nothing is counted against it")


def _selfcheck_dir(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("PIP_CACHE_DIR"):
        return Path(env["PIP_CACHE_DIR"]) / "selfcheck"
    cache = env.get("XDG_CACHE_HOME") or str(Path(env.get("HOME") or Path.home()) / ".cache")
    return Path(cache) / "pip" / "selfcheck"


def _stamp(v) -> float | None:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def pip_runs(now: float, env: dict | None = None) -> tuple[list[tuple[float, str]], str]:
    """The (time, interpreter prefix) of each pip run in the CAUSE_WINDOW_S before `now`, newest first,
    from pip's selfcheck cache, and why the cache could not be read ("" when it could, or is absent)."""
    d = _selfcheck_dir(env)
    try:
        names = list(os.scandir(d))
    except FileNotFoundError:
        return [], ""
    except OSError as e:
        return [], f"pip's selfcheck cache {d} is unreadable ({e.strerror or e})"
    out = []
    for e in names:
        try:
            if not e.is_file() or e.stat().st_mtime < now - CAUSE_WINDOW_S - 60:
                continue
            j = json.loads(Path(e.path).read_text()[:65536])
        except (OSError, ValueError):
            continue
        ts = _stamp(j.get("last_check")) if isinstance(j, dict) else None
        if ts is not None and j.get("key") and now - CAUSE_WINDOW_S <= ts <= now + 60:
            out.append((ts, str(j["key"])))
    return sorted(out, reverse=True), ""


def _names(log: Path, needles: list[str]) -> list[str]:
    """Which of `needles` the log names, reading at most _LOG_SCAN bytes from each end."""
    try:
        with open(log, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            text = fh.read(_LOG_SCAN)
            if size > _LOG_SCAN:
                fh.seek(max(_LOG_SCAN, size - _LOG_SCAN))
                text += fh.read(_LOG_SCAN)
    except OSError:
        return []
    return [n for n in needles if n.encode() in text]


def _job_folders(p) -> list[Path]:
    """The run folders (state/runs) of this project and of every project in the account's registry."""
    from .project import FOLDER, load_registry
    out = [Path(p.runs)] if getattr(p, "runs", None) else []
    try:
        reg = load_registry().get("projects") or {}
    except Exception:
        reg = {}
    for e in reg.values() if isinstance(reg, dict) else []:
        if isinstance(e, dict) and e.get("dir"):
            r = Path(e["dir"]) / FOLDER / "state" / "runs"
            if r not in out:
                out.append(r)
    return out


def jobs_naming(p, prefixes: list[str], now: float) -> list[tuple[dict, str, list[str]]]:
    """(record, project folder, prefixes its log names) of each detached job that ran in the
    CAUSE_WINDOW_S before `now` and whose log names one of `prefixes`."""
    out = []
    for runs in _job_folders(p) if prefixes else []:
        try:
            regs = [r for r in runs.glob("*/detached.json") if r.stat().st_mtime >= now - 86400]
        except OSError:
            continue
        for r in regs:
            try:
                jobs = json.loads(r.read_text())
            except (OSError, ValueError):
                continue
            for j in jobs if isinstance(jobs, list) else []:
                if not isinstance(j, dict) or not j.get("log"):
                    continue
                start, end = j.get("started") or 0, j.get("ended")
                if start > now + 60 or (isinstance(end, (int, float)) and end < now - CAUSE_WINDOW_S):
                    continue
                if hit := _names(Path(j["log"]), prefixes):
                    out.append((j, str(runs.parent.parent.parent), hit))
                    if len(out) >= CAUSE_MAX:
                        return out
    return out


def cause(p, now: float | None = None, env: dict | None = None) -> str:
    """One sentence naming a runner fault's likely cause, or saying none was found. Never raises."""
    now = time.time() if now is None else now
    try:
        def at(ts):
            try:
                from . import timefmt
                return timefmt.short(ts, p, now)
            except Exception:
                return time.strftime("%H:%M UTC", time.gmtime(ts))
        runs, why = pip_runs(now, env)
        mins = CAUSE_WINDOW_S // 60
        if why:
            return f" Likely cause: not read: {why}."
        if not runs:
            return f" Likely cause: no pip run in the {mins} minutes before (pip's selfcheck cache)."
        shown = runs[:CAUSE_MAX]
        s = (f" Likely cause: pip ran in the {mins} minutes before, from "
             + ", ".join(f"{k} at {at(ts)}" for ts, k in shown)
             + (f" and {len(runs) - len(shown)} more" if len(runs) > len(shown) else "")
             + " (pip's selfcheck cache).")
        jobs = jobs_naming(p, list(dict.fromkeys(k for _, k in shown)), now)
        for j, proj, hit in jobs:
            who = ", ".join(x for x in (f"task #{j['task']}" if j.get("task") else "",
                                        f"run {j['run']}" if j.get("run") else "") if x)
            s += (f" Detached job {j.get('name')} of {Path(proj).name}" + (f" ({who})" if who else "")
                  + f", log {j['log']}, names {', '.join(hit)}.")
        if not jobs:
            s += " No tt-project detached job's log names those interpreters."
        return s
    except Exception as e:   # the diagnosis never stops the record
        return f" Likely cause: not read ({type(e).__name__})."


def record(p, f: dict, task: str | None = None, queue: bool = False) -> bool:
    """Keep the fault open in the project's state, and raise one coordinator observation the first
    time its signature is seen in this episode. True when this call raised it. Never raises.
    `queue`: the push queue's batch found it, so the queue holds on it (held); a fault a task found
    in its own checkout never holds the queue, and does not replace how the queue's is probed."""
    if p is None:
        return False
    try:
        db = p.db
        now = time.time()
        sig0 = str(f["sig"])
        why = (cause(p, now) if sig0.startswith((PREFIX, OLD)) and f.get("kind") != "host"
               and sig0 not in open_faults(p) else "")   # read before the transaction, not in it
        with db.tx():
            faults = db.kv(KV, {}) or {}
            sig = str(f["sig"])
            old = faults.get(sig) if isinstance(faults.get(sig), dict) else None
            tasks = list((old or {}).get("tasks") or [])
            if task and task not in tasks:
                tasks = (tasks + [task])[-20:]
            keep = {k: f.get(k) for k in ("kind", "name", "python", "module", "path", "cwd", "cmd", "runner",
                                          "min", "version", "host", "why", "lock") if f.get(k) is not None}
            if old and old.get("queue") and not queue:
                keep = {k: old[k] for k in keep.keys() | old.keys() if k in old}
            faults[sig] = {**keep, "since": (old or {}).get("since") or now, "last": now, "tasks": tasks,
                           "checked": now, **({"queue": True} if queue or (old or {}).get("queue") else {})}
            db.set_kv(KV, faults)
            if old:
                return False
            fp = f"{sig}:{now:.0f}"
            py = f" with {f['python']}" if f.get("python") else ""
            what, fix = ((f"the check host {f.get('host')} cannot run checks over ssh"
                          + (f" ({f['why']})" if f.get("why") else ""),
                          "Make the host reachable by ssh without a prompt, with git and tar, or change "
                          "delivery.checks_host or the check's host")
                         if f.get("kind") == "host" else
                         (f"the checks' pinned Python env cannot be made from {f.get('lock')} ({f.get('why')})",
                          "Restore what its build needs on the host (python3's venv module, the package index "
                          "pip reads, or the lock file)")
                         if f.get("kind") == "pyenv" else
                         (f"the check runner {f.get('runner')} cannot be found or imported{py}",
                          "Restore the runner on the host (reinstall it for that python, or point the checks "
                          "at a venv)"))
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                 (now, "checks", "observation", fp, "high",
                  f"Environment fault {sig}: {what} (first seen in {f.get('cmd')!r}). Check runs wait instead "
                  "of failing and nothing is counted against any change; the push queue holds its batch. "
                  f"{fix}.{why} Waiting tasks and the queue resume once `ttp checks --env-ok` passes.", "queued"))
        return True
    except Exception:   # a note about the host never stops the caller from waiting
        return False


def open_faults(p) -> dict:
    try:
        v = p.db.kv(KV, {}) or {}
    except Exception:
        return {}
    return {k: f for k, f in v.items() if isinstance(f, dict)} if isinstance(v, dict) else {}


def _still(f: dict) -> dict | None:
    """The fault `f` as its command, looked at again in the checkout it was found in, still hits it;
    None once it does not (the runner is back, or the command now resolves another one) or once that
    checkout is gone (a removed worktree and its venv: nothing there to wait for)."""
    if f.get("kind") == "host":   # the host, wherever the check was started
        return probe(f)
    if f.get("kind") == "pyenv":   # its build, started in the background when none runs: never waited for
        from . import runnerenv
        lock = Path(str(f.get("lock") or ""))
        # one found under another Python than this process's env uses is not this host's fault here
        return None if not lock.is_file() or not runnerenv.applies(f) or runnerenv.ready(lock) else f
    cwd = str(f.get("cwd") or "")
    if not cwd or not Path(cwd).is_dir():
        return None
    if not f.get("cmd"):
        return probe(f)
    now = precheck([str(f["cmd"])], cwd, {**os.environ, "PATH": str(f.get("path") or os.defpath)})
    return now if now and now.get("sig") == f.get("sig") else None


def recheck(p, now: float | None = None, every_s: float = 0, sigs=None) -> dict:
    """Probe each open fault again (skipping those probed within `every_s`), close the ones that
    pass and return those still open. `sigs`: only those faults (a waiting task's own), the rest
    are neither probed nor closed nor returned."""
    now = time.time() if now is None else now
    faults = open_faults(p)
    if sigs:
        faults = {k: f for k, f in faults.items() if k in set(sigs)}
    if not faults:
        return {}
    still, changed = {}, False
    for sig, f in faults.items():
        if every_s and now - float(f.get("checked") or 0) < every_s:
            still[sig] = f
            continue
        changed = True
        if _still({**f, "sig": sig}):
            still[sig] = {**f, "checked": now}
    if changed:
        try:
            with p.db.tx():
                cur = p.db.kv(KV, {}) or {}
                # A fault another caller recorded meanwhile stays.
                cur = {k: v for k, v in cur.items() if k in still or k not in faults}
                cur.update(still)
                p.db.set_kv(KV, cur)
        except Exception:
            pass
    return still


def held(p, now: float | None = None) -> str:
    """Why the push queue holds (an open fault its own batch found, whose runner is still missing),
    or "". A hold past HOLD_ALERT_S raises one more coordinator observation."""
    if not any(f.get("queue") for f in open_faults(p).values()):
        return ""
    now = time.time() if now is None else now
    still = {k: f for k, f in recheck(p, now, PROBE_S).items() if f.get("queue")}
    if not still:
        return ""
    long = [k for k, f in still.items() if now - float(f.get("since") or now) > HOLD_ALERT_S and not f.get("alerted")]
    if long:
        try:
            with p.db.tx():
                cur = p.db.kv(KV, {}) or {}
                for k in long:
                    if isinstance(cur.get(k), dict):
                        cur[k]["alerted"] = now
                p.db.set_kv(KV, cur)
                p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                       (now, "checks", "observation", f"env-hold:{','.join(sorted(long))}:{now:.0f}", "high",
                        f"The push queue has held for over {HOLD_ALERT_S // 3600} h on environment fault "
                        f"{', '.join(sorted(long))}: no batch runs until the runner is back on the host "
                        "(`ttp checks --env-ok` passes).", "queued"))
        except Exception:
            pass
    return "held: environment fault " + ", ".join(sorted(still)) + " (a check runner or check host is missing)"


def retry_when(p, f: dict | None = None) -> str:
    """The probe a task waiting on fault `f` hands off: it rechecks only that fault, so another
    task's fault that never clears keeps it asleep no longer than its own."""
    from . import push
    sig = f" {shlex.quote(str(f['sig']))}" if f and f.get("sig") else ""
    return (f"{push._own_ttp(p)} checks --env-ok" if p else "ttp checks --env-ok") + sig


def write_marker(run_dir: str | Path | None, f: dict) -> None:
    if not run_dir:
        return
    from .project import write_json
    try:
        write_json(Path(run_dir) / MARKER, {**f, "ts": time.time()})
    except OSError:
        pass


MARKER = "env_fault.json"   # in a run's directory: its checks stopped at a runner fault


def marker_open(run_dir: Path) -> dict | None:
    """The fault a run's checks stopped at, while its runner is still missing; else None."""
    import json
    try:
        f: Any = json.loads((Path(run_dir) / MARKER).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(f, dict):
        return None
    return _still(f)
