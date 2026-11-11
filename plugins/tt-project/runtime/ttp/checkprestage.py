"""`ttp checks --detach` pre-stage: a short command a project names in delivery.check_prestage that
runs in the foreground before the full checks are started in the background. A failure there is
found in the same run (the worker fixes it at once) instead of after the whole detached suite, which
costs a hand-off and a later wake. It only adds an early run: the full checks still run every
command unchanged, and nothing it does marks a check as passed. A timeout, a pre-stage that cannot
be started or a malformed setting never holds the full checks back. It runs on a host test slot
when one is free within SLOT_WAIT_S, else on one worker without a slot: it is small and bounded, and
a busy host is when a full run it saves costs most.

With delivery.check_prestage unset, a project whose own checks run pytest here gets a derived pre-stage
(`derive`): the DERIVE_MAX tests that failed in the most distinct detached `ttp checks` runs of the
project over DERIVE_WINDOW_S, leaving out those a run recorded taking DERIVE_FAST_S or more. A derived
pre-stage that times out records the tests it cut off as slow (`_learn_from_timeout`), so the next one
leaves them out instead of timing out the same way. A derived pre-stage that fails reruns its failing
tests on the change's base in a clean checkout (`_shared_with_base`): when every one fails there too
(a broken push-branch tip, which the derived list then mostly holds), the full checks start, since
failures the base shares never block a change. No such failures: no pre-stage. `''` or false turns it
off; an explicit command is used as it is."""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shlex
import tempfile
import time
from pathlib import Path

from . import boundedchild, testslots

KEY = "check_prestage"
DEFAULT_TIMEOUT_S = 90.0     # under the 120 s a Bash tool call gets by default, so the caller outlives it
SLOT_WAIT_S = 5.0   # the longest it waits for a test slot before it runs on one worker without one
MAX_TIMEOUT_S = 900.0
TAIL_LINES = 60
CANNOT_RUN = (126, 127)   # the shell found no such command or could not run it: a setting fault
DERIVE_MAX, DERIVE_WINDOW_S, DERIVE_FAST_S = 10, 14 * 86400, 5.0
DERIVED_KEY, CAUGHT_KEY = "check_prestage_derived", "check_prestage_caught"   # kv, for the daily audit
FAILURE = "check_failure.json"   # a `ttp checks` run's last failure, in its run's directory
DURATIONS = "prestage-slowest-tests.json"   # the derived pre-stage's own test times, beside slowtests.FILE
LIVE = "prestage-test-times.jsonl"   # its tests' starts and ends as they happen (the durations plugin)
NO_TESTS, SETUP = 5, (3, 4)   # pytest: no test selected (a pass here); internal and usage errors (never a failure here)


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


def run_derived(p, ids: list[str], checks: list, top: str, env: dict, say, run_dir: str | Path | None = None,
                base: str = "") -> int:
    """Run the derived pre-stage `ids` as `run` does (0: start the full checks, 1: start nothing),
    the way the project's pytest check among `checks` runs (push.check_argv: its venv, its "env"
    exports, and its process sandbox unless it opts out). No test selected passes, and a pytest internal or usage error never
    holds the full checks back; a failure is
    recorded for the daily audit (CAUGHT_KEY). Its test times go to DURATIONS in `run_dir`, so a
    test found slow there is left out next time; on a timeout, from each test's end as it happened,
    with the tests cut off recorded as slow (`_learn_from_timeout`). Given `base` (the commit the change
    was built on), a failure whose failing tests all fail on `base` too starts the full checks
    (`_shared_with_base`)."""
    from . import push, pushpass, slowtests
    spec = derived_command(ids, top)
    if spec is None:
        say("pre-stage skipped: none of the derived tests' files exist on this head")
        return 0
    cmd, pattern = spec
    env = {**env, **pushpass.select_env(pattern, env)}
    learn = None
    if run_dir:
        live = Path(run_dir) / LIVE
        try:
            live.unlink()
        except OSError:
            pass
        env = {**env, **slowtests.env(Path(run_dir) / DURATIONS, env, live=live)}
        learn = lambda took: _learn_from_timeout(Path(run_dir), ids, took, say)
    check = _check(cmd, checks)
    shared = (lambda out: _shared_with_base(p, out, checks, top, env, base, say)) if base else None
    rc = run(shlex.join(push.check_argv(check, top, env)), top, env, say,
             what="derived from this project's frequent check failures", ok=(0, NO_TESTS),
             cannot=(*SETUP, *CANNOT_RUN), shown=cmd, on_timeout=learn, on_fail=shared)
    if rc:
        _record(p, CAUGHT_KEY, lambda rows: [*[r for r in rows or [] if isinstance(r, dict)
                                                and r.get("ts", 0) >= time.time() - DERIVE_WINDOW_S],
                                              {"ts": time.time(), "ids": ids}])
    return rc


def _check(cmd: str, checks: list):
    """`cmd` as a check run the way the project's pytest check among `checks` runs: with its exports
    and its sandbox opt-out."""
    from . import push
    check = push.Check(cmd)
    check.env = tuple(getattr(pytest_check(checks), "env", ()) or ())
    check.sandbox = getattr(pytest_check(checks), "sandbox", True) is not False
    return check


def _shared_with_base(p, output: str, checks: list, top: str, env: dict, base: str, say) -> bool:
    """Whether a failed derived pre-stage's failing tests (from its `output`) all fail on `base` too:
    each reruns there, in a clean checkout of `base` made beside the repository at `top`, the way the
    pre-stage ran. True starts the full checks: a failure the base shares (a broken push-branch tip)
    is not the change's, and the full checks' own base rerun judges it again. False (the failure
    stands) when the output does not name every failing test, a test passes on the base, or the base
    rerun cannot tell (cannot start, times out, names no failure)."""
    from . import cleantree, push, pushpass, sharedchecks, worktree
    ids, whole = sharedchecks.failing_ids(output)
    if not ids or not whole:
        return False
    say(f"pre-stage: rerunning its {len(ids)} failing test(s) on the base {base[:12]} to see whether the base "
        f"fails them too: {', '.join(ids)}")
    tree = None
    try:
        tree = cleantree.Checkout(top, base)
        where = str(tree.path)
        if p is not None:
            worktree.prepare(p, where)
        spec = derived_command(ids, where)
        if spec is None:
            say("pre-stage: none of the failing tests' files exist on the base; the failure stands")
            return False
        cmd, pattern = spec
        e = {**env, "PWD": where, **pushpass.select_env(pattern, env, ids)}
        argv = shlex.join(push.check_argv(_check(cmd, checks), where, e))
        with tempfile.NamedTemporaryFile(prefix="ttp-prestage-base-") as out:
            script = f"exec >{shlex.quote(out.name)} 2>&1\n{testslots.clamped(argv, 1)}"
            res = boundedchild.run(testslots.low_priority(["/bin/sh", "-c", script]), DEFAULT_TIMEOUT_S, cwd=where,
                                   env={**e, testslots.XDIST_ENV: "1", testslots.SLOTS_ENV: "1"}, text=False,
                                   stderr="stdout")
            out.seek(0)
            got = out.read().decode(errors="replace")
    except (cleantree.Error, OSError, ValueError) as err:
        say(f"pre-stage: the base rerun could not run ({err}); the failure stands")
        return False
    finally:
        if tree is not None:
            tree.close()
    if res.timed_out or res.error:
        say(f"pre-stage: the base rerun could not tell ({res.error or 'timed out'}); the failure stands")
        return False
    on_base = set(sharedchecks.failing_ids(got)[0]) if res.rc else set()
    new = [i for i in ids if i not in on_base]
    if new:
        say(f"pre-stage: {len(new)} of its failing test(s) pass on the base {base[:12]}, so the change "
            f"broke them: {', '.join(new)}")
        return False
    say(f"pre-stage: every failing test also fails on the base {base[:12]}, so none is this change's; "
        f"starting the full checks: {', '.join(ids)}")
    return True


def _learn_from_timeout(run_dir: Path, ids: list[str], took: float, say) -> None:
    """After a derived pre-stage timed out: merge into DURATIONS the times of the tests that ended
    (LIVE) and, for each test still running when it was stopped, its time so far but at least
    DERIVE_FAST_S, so `derive` leaves it out next time. A test that never started is not guessed
    slow, unless none started at all (collection or setup outlasted the timeout): then every
    derived id is. A fault here never touches the checks."""
    ended: dict[str, float] = {}
    began: dict[str, float] = {}
    try:
        text = (run_dir / LIVE).read_text(errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        try:
            row = json.loads(line)
            i = str(row["id"])
            if "s" in row:
                ended[i] = float(row["s"])
            else:
                began[i] = float(row["start"])
        except (ValueError, TypeError, KeyError):
            continue   # a line cut off by the stop
    now = time.time()
    cut = {i: max(now - t, DERIVE_FAST_S) for i, t in began.items() if i not in ended}
    if not began:
        cut = {i: max(took, DERIVE_FAST_S) for i in ids}
    if not cut and not ended:
        return
    path = run_dir / DURATIONS
    try:
        from . import project, slowtests
        times = dict(slowtests.load(path)) | ended | cut
        project.durable_write(path, json.dumps({"tests": [{"id": i, "s": round(t, 3)} for i, t in times.items()],
                                                "count": len(times)}))
    except Exception:
        return
    if cut:
        say(f"pre-stage left out of the next derived list, cut off by the timeout: {', '.join(sorted(cut))}")


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
        cannot: tuple = CANNOT_RUN, shown: str = "", on_timeout=None, on_fail=None) -> int:
    """Run the pre-stage `v` in `cwd`; `say` prints a line. 0: start the full checks (it passed,
    timed out, is unset, malformed or could not be started); 1: it failed, start nothing. `ok` exit
    codes pass and `cannot` ones are a setting fault (`what` names the setting); `shown` is the
    command as printed (default `v`'s); `on_timeout(seconds)` runs once a timed-out one is stopped;
    `on_fail(output)` gets a failed one's whole output, and True from it starts the full checks."""
    try:
        spec = parse(v)
    except ValueError as e:
        say(f"delivery.{KEY} ignored: {e}; starting the full checks without it")
        return 0
    if spec is None:
        return 0
    cmd, timeout = spec
    # It runs on the host's test slots (testslots) like the full checks; none free within SLOT_WAIT_S,
    # on one worker without a slot, so a busy host never skips it (a full run it saves takes a slot).
    grant = testslots.acquire(f"ttp checks pre-stage (task #{os.environ.get('TTP_TASK') or '?'}, pid {os.getpid()})",
                              say, SLOT_WAIT_S)
    k, slot_env = (grant.k, grant.env()) if grant is not None \
        else (1, {testslots.XDIST_ENV: "1", testslots.SLOTS_ENV: "1"})
    if grant is None:
        say("the host's test slots are busy: the pre-stage runs on one worker without a slot")
    say(f"pre-stage {(shown or cmd)!r} ({what}, up to {timeout:.0f} s) before the full checks start in the background")
    t0 = time.monotonic()
    # Its output goes to a file, not a pipe, so a process it leaves running cannot hold the run open;
    # boundedchild kills its whole group however the run ends (a SIGTERM or SIGHUP to `ttp checks` too).
    with grant or contextlib.nullcontext(), tempfile.NamedTemporaryFile(prefix="ttp-prestage-") as out:
        script = f"exec >{shlex.quote(out.name)} 2>&1\n{testslots.clamped(cmd, k)}"
        res = boundedchild.run(testslots.low_priority(["/bin/sh", "-c", script]), timeout, cwd=cwd,
                               env={**env, **slot_env}, text=False, stderr="stdout")
        if res.error.startswith("could not start"):
            say(f"pre-stage not started ({res.error.partition(': ')[2]}); starting the full checks without it")
            return 0
        if res.timed_out:
            say(f"pre-stage timed out after {timeout:.0f} s; starting the full checks anyway")
            if on_timeout:
                on_timeout(time.monotonic() - t0)
            return 0
        rc, took = res.rc, time.monotonic() - t0
        if rc in ok:
            say(f"pre-stage passed in {took:.0f} s")
            return 0
        if rc in cannot:
            why = f"{what} names a command this host lacks" if what == f"delivery.{KEY}" else what
            say(f"pre-stage could not run (exit {rc}): {why}; starting the full checks without it")
            return 0
        out.seek(0)
        raw = out.read()
        tail = _tail(raw)
    last = f" Its output's last {TAIL_LINES} lines:\n{tail}\nttp checks:"
    if on_fail is not None:   # its output shown once, before the base rerun's lines
        say(f"pre-stage failed (exit {rc}) in {took:.0f} s.{last.removesuffix(chr(10) + 'ttp checks:')}")
        if on_fail(raw.decode(errors="replace")):
            return 0
        last = ""
    say(f"pre-stage failed (exit {rc}) in {took:.0f} s; the full checks were NOT started.{last} Fix this and "
        "commit, then run `ttp checks --detach` again "
        "in this run. If the pre-stage itself is broken, not the change, `--no-prestage` starts the full checks "
        "without it")
    return 1

