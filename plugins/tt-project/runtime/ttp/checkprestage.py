"""`ttp checks --detach` pre-stage: a short command a project names in delivery.check_prestage that
runs in the foreground before the full checks are started in the background. A failure there is
found in the same run (the worker fixes it at once) instead of after the whole detached suite, which
costs a hand-off and a later wake. It only adds an early run: the full checks still run every
command unchanged, and nothing it does marks a check as passed. A timeout, a pre-stage that cannot
be started or a malformed setting never holds the full checks back.

With delivery.check_prestage unset, a project whose own checks run pytest here gets a derived pre-stage
(`derive`): the DERIVE_MAX tests that failed in the most distinct detached `ttp checks` runs of the
project over DERIVE_WINDOW_S, leaving out those a run recorded taking DERIVE_FAST_S or more. No such
failures: no pre-stage. `''` or false turns it off; an explicit command is used as it is."""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

KEY = "check_prestage"
DEFAULT_TIMEOUT_S = 90.0     # under the 120 s a Bash tool call gets by default, so the caller outlives it
MAX_TIMEOUT_S = 900.0
TAIL_LINES = 60
CANNOT_RUN = (126, 127)   # the shell found no such command or could not run it: a setting fault
STOP_ON = (signal.SIGTERM, signal.SIGHUP)   # what ends `ttp checks` (a tool timeout, a run's group kill)
DERIVE_MAX, DERIVE_WINDOW_S, DERIVE_FAST_S = 10, 14 * 86400, 5.0
DERIVED_KEY, CAUGHT_KEY = "check_prestage_derived", "check_prestage_caught"   # kv, for the daily audit
FAILURE = "check_failure.json"   # a `ttp checks` run's last failure, in its run's directory
DURATIONS = "prestage-slowest-tests.json"   # the derived pre-stage's own test times, beside slowtests.FILE
NO_TESTS, SETUP = 5, (3, 4)   # pytest: no test selected (a pass here); internal and usage errors (never a failure here)


class _Signalled(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def parse(v) -> tuple[str, float] | None:
    """(command, timeout) from delivery.check_prestage: a command string, or {"run", "timeout_s"}.
    None when unset or empty; ValueError when malformed."""
    if v is None or v is False or v == "" or v == {}:
        return None
    if isinstance(v, str):
        return (v, DEFAULT_TIMEOUT_S) if v.strip() else None
    if not isinstance(v, dict):
        raise ValueError("not a command string or a {\"run\", \"timeout_s\"} object")
    if extra := set(v) - {"run", "timeout_s"}:
        raise ValueError(f"unknown key(s) {', '.join(sorted(map(str, extra)))}")
    run, t = v.get("run"), v.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(run, str) or not run.strip():
        raise ValueError("\"run\" is not a non-empty command string")
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 < t <= MAX_TIMEOUT_S:
        raise ValueError(f"\"timeout_s\" is not a number of seconds in (0, {MAX_TIMEOUT_S:.0f}]")
    return run, float(t)


def problems(v) -> list[str]:
    try:
        parse(v)
    except ValueError as e:
        return [f"delivery.{KEY}: {e}"]
    return []


def derive(runs: Path, now: float | None = None) -> list[str]:
    """Up to DERIVE_MAX pytest ids that failed in the most distinct detached `ttp checks` runs under
    `runs` (a project's run directories) over DERIVE_WINDOW_S, most first; a test some run recorded
    taking DERIVE_FAST_S or more (slowtests, or a derived pre-stage's own DURATIONS) is left out. A
    run's directory keeps its last failure (FAILURE) and, once it detached, the detached checks' pid file."""
    from . import sharedchecks, slowtests
    from .cli import CHECKS_PID
    since = (time.time() if now is None else now) - DERIVE_WINDOW_S
    runs_of: dict[str, int] = {}
    slow: set[str] = set()
    try:
        dirs = list(Path(runs).iterdir())
    except OSError:
        return []
    for d in dirs:
        for durations in (d / slowtests.FILE, d / DURATIONS):
            try:
                if durations.stat().st_mtime >= since:
                    slow.update(i for i, sec in slowtests.load(durations) if sec >= DERIVE_FAST_S)
            except OSError:
                pass
        try:
            if (d / FAILURE).stat().st_mtime < since or not (d / CHECKS_PID).exists():
                continue
            fail = json.loads((d / FAILURE).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(fail, dict) or fail.get("kind") != "check_failed" \
                or fail.get("purpose", "candidate") != "candidate":
            continue
        for i in sharedchecks.failing_ids(str(fail.get("output") or ""))[0]:
            runs_of[i] = runs_of.get(i, 0) + 1
    ranked = sorted((i for i in runs_of if i not in slow), key=lambda i: (-runs_of[i], i))
    return ranked[:DERIVE_MAX]


def pytest_check(checks: list):
    """The first check that runs pytest here: on this host (no remote host), at the repository's top."""
    from . import push
    return next((c for c in checks if push._PYTEST_RUN.search(str(c)) and getattr(c, "host", "") in ("", "local")
                 and not getattr(c, "cwd", "")), None)


def derived_command(ids: list[str], top: str) -> tuple[str, str] | None:
    """(command, name pattern) running `ids` with pytest at `top`: the files of those that still exist,
    keeping only the tests so named (the ttp_pytest_select plugin), so a test since removed or renamed
    is deselected and never an error (pytest refuses a whole run naming one missing id). None when
    none of their files is left."""
    kept = [i for i in ids if not Path(f := i.partition("::")[0]).is_absolute() and ".." not in Path(f).parts
            and (Path(top) / f).is_file()]
    if not kept:
        return None
    files = sorted({i.partition("::")[0] for i in kept})
    names = sorted({i.rpartition("::")[2] for i in kept})
    return ("python3 -m pytest -q -p no:cacheprovider " + " ".join(map(shlex.quote, files)),
            "^(?:" + "|".join(map(re.escape, names)) + ")$")


def run_derived(p, ids: list[str], checks: list, top: str, env: dict, say, run_dir: str | Path | None = None) -> int:
    """Run the derived pre-stage `ids` as `run` does (0: start the full checks, 1: start nothing),
    the way the project's pytest check among `checks` runs (push.check_argv: its venv, and its process
    sandbox unless it opts out). No test selected passes, and a pytest internal or usage error never
    holds the full checks back; a failure is
    recorded for the daily audit (CAUGHT_KEY). Its test times go to DURATIONS in `run_dir`, so a
    test found slow there is left out next time."""
    from . import push, pushpass, slowtests
    spec = derived_command(ids, top)
    if spec is None:
        say("pre-stage skipped: none of the derived tests' files exist on this head")
        return 0
    cmd, pattern = spec
    env = {**env, **pushpass.select_env(pattern, env)}
    if run_dir:
        env = {**env, **slowtests.env(Path(run_dir) / DURATIONS, env)}
    isolate = getattr(pytest_check(checks), "sandbox", True) is not False
    rc = run(shlex.join(push.check_argv(cmd, top, env, isolate)), top, env, say,
             what="derived from this project's frequent check failures", ok=(0, NO_TESTS),
             cannot=(*SETUP, *CANNOT_RUN), shown=cmd)
    if rc:
        _record(p, CAUGHT_KEY, lambda rows: [*[r for r in rows or [] if isinstance(r, dict)
                                                and r.get("ts", 0) >= time.time() - DERIVE_WINDOW_S],
                                              {"ts": time.time(), "ids": ids}])
    return rc


def derived(p, checks: list, say) -> list[str] | None:
    """The derived pre-stage's ids when delivery.check_prestage is unset (printed, and kept for the
    daily audit); None when it does not apply (no project, or no check runs pytest here)."""
    if p is None or pytest_check(checks) is None:
        return None
    ids = derive(p.runs)
    say(f"pre-stage derived from this project's detached checks failures (delivery.{KEY} unset): "
        + (", ".join(ids) if ids else f"none in the last {DERIVE_WINDOW_S // 86400} days, so none runs"))
    _record(p, DERIVED_KEY, lambda _: {"ts": time.time(), "ids": ids})
    return ids


def _record(p, key: str, update) -> None:
    """kv[key] = update(kv[key]); a state fault never touches the checks."""
    try:
        p.db.set_kv(key, update(p.db.kv(key)))
    except Exception:
        pass


def audit_line(db, since: float) -> str:
    """The daily audit's line: the last derived list and the failures it caught before detaching in
    the window (each a detached run and a worker wake saved); '' when none was derived."""
    last = db.kv(DERIVED_KEY)
    if not isinstance(last, dict):
        return ""
    ids = last.get("ids") or []
    caught = [r for r in db.kv(CAUGHT_KEY) or [] if isinstance(r, dict) and r.get("ts", 0) >= since]
    return (f"derived check pre-stage: {len(ids)} test(s)" + (f" ({', '.join(ids)})" if ids else "")
            + f"; caught {len(caught)} failure(s) before detaching ({len(caught)} wake(s) saved)")


def _tail(raw: bytes, n: int = TAIL_LINES) -> str:
    lines = raw.decode(errors="replace").rstrip("\n").splitlines()
    return "\n".join(lines[-n:])


def run(v, cwd: str, env: dict, say, what: str = f"delivery.{KEY}", ok: tuple = (0,),
        cannot: tuple = CANNOT_RUN, shown: str = "") -> int:
    """Run the pre-stage `v` in `cwd`; `say` prints a line. 0: start the full checks (it passed,
    timed out, is unset, malformed or could not be started); 1: it failed, start nothing. `ok` exit
    codes pass and `cannot` ones are a setting fault (`what` names the setting); `shown` is the
    command as printed (default `v`'s)."""
    try:
        spec = parse(v)
    except ValueError as e:
        say(f"delivery.{KEY} ignored: {e}; starting the full checks without it")
        return 0
    if spec is None:
        return 0
    cmd, timeout = spec
    say(f"pre-stage {(shown or cmd)!r} ({what}, up to {timeout:.0f} s) before the full checks start in the background")
    t0 = time.monotonic()
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            say(f"pre-stage not started ({e}); starting the full checks without it")
            return 0
        try:
            rc = _wait(proc, timeout)
        except subprocess.TimeoutExpired:
            rc = None
        if rc is None:
            _stop(proc)
            say(f"pre-stage timed out after {timeout:.0f} s; starting the full checks anyway")
            return 0
        took = time.monotonic() - t0
        if rc in ok:
            say(f"pre-stage passed in {took:.0f} s")
            return 0
        if rc in cannot:
            why = f"{what} names a command this host lacks" if what == f"delivery.{KEY}" else what
            say(f"pre-stage could not run (exit {rc}): {why}; starting the full checks without it")
            return 0
        out.seek(0)
        tail = _tail(out.read())
    say(f"pre-stage failed (exit {rc}) in {took:.0f} s; the full checks were NOT started. Its output's last "
        f"{TAIL_LINES} lines:\n{tail}\nttp checks: fix this and commit, then run `ttp checks --detach` again "
        "in this run. If the pre-stage itself is broken, not the change, `--no-prestage` starts the full checks "
        "without it")
    return 1


def _wait(proc: subprocess.Popen, timeout: float) -> int:
    """proc.wait(timeout), but a SIGTERM or SIGHUP to this process, or any interruption, first ends
    the pre-stage's group: it runs in a session of its own, so the signal that ends `ttp checks`
    never reaches it. The signal then takes its course as if no handler had been set."""
    old: dict = {}
    def caught(signum, frame):
        raise _Signalled(signum)
    try:
        for sig in STOP_ON:
            if signal.getsignal(sig) is not signal.SIG_IGN:
                old[sig] = signal.signal(sig, caught)
    except ValueError:   # not the main thread: no handlers, the except below still covers it
        pass
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise
    except BaseException as e:   # interrupted: never leave its process group running
        _stop(proc)
        if not isinstance(e, _Signalled):
            raise
        prior = old.get(e.signum, signal.SIG_DFL)
        for sig, h in old.items():
            signal.signal(sig, h if h is not None else signal.SIG_DFL)
        old = {}
        if callable(prior):
            prior(e.signum, None)
        else:
            signal.signal(e.signum, signal.SIG_DFL)
            os.kill(os.getpid(), e.signum)
        raise SystemExit(128 + e.signum)
    finally:
        for sig, h in old.items():
            signal.signal(sig, h if h is not None else signal.SIG_DFL)


def _stop(proc: subprocess.Popen) -> None:
    """End the pre-stage's own process group (this command started it in a session of its own)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue
