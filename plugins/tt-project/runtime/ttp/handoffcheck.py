# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The project's own hand-off checks (project.json `worker.handoff_checks`): model-free commands the
daemon runs on each worker's hand-off before it accepts it (Daemon._finish_worker), so a rule that
must hold every time is tested before a reply posts or a review or push is queued, not after.

Each entry is {name, run, timeout_s (60), kinds (all)}. `run` is the project's own shell command, run
from the project root; the hand-off's path is in TTP_RESULT_JSON (with TTP_TASK_ID and TTP_TASK_KIND)
and replaces a `{result}` placeholder, shell-quoted. Nothing else from the task reaches the command
line. Exit 0 passes; 1 fails: the run is sent back with the check's output, at most
`worker.handoff_check_max_sendbacks` times (2) per task, then the task goes to review for the
coordinator, its result kept. Any other exit or a timeout is a broken check: the hand-off is accepted
(a broken hook never costs work or a message) and the coordinator hears once per check signature.

Checks run on a hand-off whose status is done, needs_review, blocked or failed (a wait posts nothing
and its later hand-off is checked). They run before the transaction that records the hand-off
(prefetch, from Daemon._prefetch_lint), so the write lock never waits on them; verdict reads that
answer and runs them itself only when prefetch did not.

They also run in the worker's own session first (at_stop, from the Claude Code or Codex Stop hook in hook.py), so
a rejection is fixed by the session that wrote the hand-off rather than by a new cold run. The daemon
passes each task run its send-backs so far and the cap (SENDBACKS_ENV, "used/cap"); a check that exits 1
refuses the stop with its output while used + the run's own refusals (STOP_FILE) stay under the cap,
and the daemon counts those refusals as send-backs. A broken check, a timeout (the checks get
STOP_BUDGET_S in all) or any fault of the hook lets the run end: the daemon's check stays the final
gate. A check sees AT_ENV=stop there, with the run's own variables (RUN_VARS) left out.

A code task's hand-off also meets one built-in check (builtin_check, no setting): it must show a run line
of its new tests failing on the base commit (new_test_gap), whatever ran it; a run on the GLM model is checked
further: its branch's added test lines must not name the real /tmp, /home, ~ or Path.home()
(glm_test_hygiene), its added comments and docstrings must state the rule and never tell what happened
(slop.new_noisy), and its hand-off must name each numbered spec step (handoff_gaps). A failure sends back like a project check, under
the same cap."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from .project import Project, durable_write

KEY = "handoff_check_sendbacks"   # in the task's result: send-backs so far
BROKEN_EVENT = "handoff_check_broken"
STATUSES = ("done", "needs_review", "blocked", "failed")
DEFAULT_TIMEOUT_S = 60
DEFAULT_MAX_SENDBACKS = 2
OUTPUT_CHARS = 2000               # the tail of a check's output the next run and the coordinator get
EVENT_OUTPUT_CHARS = 1000         # of it, in a coordinator event
INPUT_FILE = "handoff_check.json"   # the hand-off checked, when it is not the run's result.json as is
SENDBACKS_ENV = "TTP_HANDOFF_CHECK_SENDBACKS"   # "used/cap", set by the daemon on a task run
STOP_FILE = "handoff_check.stops"   # in the run dir: the stops the hook refused on a failed check
AT_ENV = "TTP_HANDOFF_CHECK_AT"   # "stop" when the Stop hook runs the check in the worker's session
STOP_BUDGET_S = 120               # all checks at a stop; one cut short is a timeout and never blocks
RUN_VARS = ("TTP_RUN_DIR", "TTP_TASK", "TTP_TASK_ID", "TTP_RUN_ID", "TTP_PROJECT", "TTP_PIDNS", "TTP_CGROUP",
            "TTP_LOCKS_HELD", "TTP_DETACHED", "TTP_OUTLIVES_RUN", "TTP_HANDOFF_GUARD", SENDBACKS_ENV)
SEEN_TTL_S = 300
KILL_WAIT_S = 5                   # the wait for a killed check's output; an escaped child is not waited for
_seen: dict[tuple[str, int, str], tuple[float, dict]] = {}
GLM_CHECK = "glm hand-off check"   # a GLM run's full built-in check
CODE_CHECK = "new-test check"        # any other run's: a new test must be shown failing on the base
# The real machine's paths in a test (a temp dir or the user's home): a string literal that opens with one, or
# Path.home(); tmp_path or monkeypatch on the line clears it. Paths in docstrings, comments, the middle of a string
# (fixture data) and an endswith/startswith comparison are text, not files the test touches.
REAL_PATH = re.compile(r"(?<!endswith\()(?<!startswith\()(?<![\w\"'])[rfbRFB]{0,2}[\"'](?:/tmp\b|/home/|~/)"
                       r"|\bPath\.home\(\)")
TEST_FILE = re.compile(r"(?:^|/)(?:tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")
NEW_TEST = re.compile(r"^\+\s*(?:async\s+)?def (test_\w+)|^\+\s*(?:it|test)\(\s*[\"'`]([^\"'`]+)")
# A run line: the new tests fail on the base, however it is named (main, the old code, a revert, a sha).
_BEFORE = r"\b(?:base|main|master|old|before|parent|revert\w*|[0-9a-f]{7,40})\b"
BASE_FAILS = re.compile(rf"(?i){_BEFORE}.{{0,120}}\bfail|\bfail.{{0,120}}{_BEFORE}")
RANGE = re.compile(r"\(?\b([a-z]|\d{1,2})\)?\s*(?:-|–|to)\s*\(?([a-z]|\d{1,2})\)")
STEP = re.compile(r"^\s*(?:\(([a-z]|\d{1,2})\)|(\d{1,2})[.)])\s+\S")   # a spec's numbered step: (a), (1), 1. or 1)


def rules(cfg: dict) -> tuple[list[dict], list[str]]:
    """The configured checks, normalised, and one problem line per entry skipped as malformed."""
    worker = cfg.get("worker") if isinstance(cfg.get("worker"), dict) else {}
    raw = worker.get("handoff_checks")
    if raw in (None, []):
        return [], []
    if not isinstance(raw, list):
        return [], ["worker.handoff_checks: not a list; ignored"]
    out, problems = [], []
    for i, e in enumerate(raw):
        where = f"worker.handoff_checks[{i}]"
        try:
            if not isinstance(e, dict):
                raise ValueError("not an object")
            run = e.get("run")
            if not isinstance(run, str) or not run.strip():
                raise ValueError("run must be a non-empty command")
            t = e.get("timeout_s", DEFAULT_TIMEOUT_S)
            if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
                raise ValueError("timeout_s must be a positive number of seconds")
            kinds = e.get("kinds")
            if isinstance(kinds, str):
                kinds = [kinds]
            if kinds is not None and (not isinstance(kinds, list) or not all(isinstance(k, str) and k for k in kinds)):
                raise ValueError("kinds must be a list of task kinds")
            name = e.get("name")
            if name is not None and not isinstance(name, str):
                raise ValueError("name must be a string")
            out.append({"name": (name or "").strip() or where, "run": run.strip(), "timeout_s": float(t),
                        "kinds": set(kinds) if kinds else None})
        except ValueError as err:
            problems.append(f"{where}: {err}; skipped")
    cap = worker.get("handoff_check_max_sendbacks")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
        problems.append(f"worker.handoff_check_max_sendbacks: {cap!r} is not a whole number >= 0; "
                        f"{DEFAULT_MAX_SENDBACKS} is used")
    return out, problems


def max_sendbacks(cfg: dict) -> int:
    v = (cfg.get("worker") or {}).get("handoff_check_max_sendbacks") if isinstance(cfg.get("worker"), dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else DEFAULT_MAX_SENDBACKS


def applies(cfg: dict, task: dict, handoff) -> list[dict]:
    """The checks that run on `task`'s hand-off: none unless it is a finished one (STATUSES)."""
    if not isinstance(handoff, dict) or handoff.get("status") not in STATUSES:
        return []
    kind = task.get("kind") or "work"
    return [c for c in rules(cfg)[0] if c["kinds"] is None or kind in c["kinds"]]


def _added(diff_text: str):
    """(path, line) for each line a unified diff adds, the line with its leading '+'."""
    path = ""
    for line in (diff_text or "").splitlines():
        if line.startswith(("diff --git", "+++ ")):
            path = line[4:].strip().removeprefix("b/") if line.startswith("+++ ") else ""
        elif line.startswith("+"):
            yield path, line


def glm_test_hygiene(diff_text: str) -> list[str]:
    """One line per added test-file line of the unified diff `diff_text` that names a real path (REAL_PATH),
    comments and lines using tmp_path or monkeypatch aside."""
    out = []
    for path, line in _added(diff_text):
        code = line[1:].split(" #")[0]
        if not TEST_FILE.search(path) or code.lstrip().startswith(("#", '"""', "'''")) \
                or "tmp_path" in code or "monkeypatch" in code:
            continue
        if m := REAL_PATH.search(code):
            real = m.group(0).lstrip("rfbRFB\"'")
            out.append(f"{path}: `{code.strip()[:160]}` uses the real {real}: use tmp_path or monkeypatch")
    return out


def new_test_gap(diff_text: str, handoff: dict) -> str | None:
    """The one line a hand-off adding tests owes: a run of them failing on the base commit, which stops a test
    that cannot fail (it already passed there, or its stub records too little)."""
    tests = [m.group(1) or m.group(2) for path, line in _added(diff_text) if TEST_FILE.search(path)
             for m in [NEW_TEST.match(line)] if m]
    told = json.dumps({k: v for k, v in handoff.items() if k != "commits"})   # its own commit ids run nothing
    if tests and not BASE_FAILS.search(told):
        return ("New tests (" + ", ".join(tests[:5]) + ") with no line showing them fail on the base commit: "
                "run them there and put the command and its failing result in your self_check.")
    return None


def handoff_gaps(diff_text: str, spec: str, handoff: dict) -> list[str]:
    """What a hand-off leaves out: a run line showing the tests the diff adds fail on the base commit, and a
    self_check naming each numbered step of the spec (its own steps, glmwork.goal_spec, two or more)."""
    out = []
    if gap := new_test_gap(diff_text, handoff):
        out.append(gap)
    from .glmwork import goal_spec
    steps = list(dict.fromkeys(m.group(1) or m.group(2) for line in goal_spec(spec).splitlines()
                               for m in [STEP.match(line)] if m))
    check = handoff.get("self_check")
    check = check if isinstance(check, str) else json.dumps(check or "")
    for a, b in RANGE.findall(check.lower()):   # "(a)-(f)" names each step between
        check += "".join(f" ({x})" for x in steps if x.isdigit() == a.isdigit() == b.isdigit()
                         and (int(a) <= int(x) <= int(b) if x.isdigit() else a <= x <= b))
    missed = [s for s in steps if not re.search(rf"(?i)(?<![\w.#])(?:\({s}\)|{s}[.):]|(?:step|part|item)\s+{s}\b)", check)]
    if len(steps) > 1 and missed:
        out.append(f"Your self_check names no spec step {', '.join(missed)}: list each numbered step with done, or "
                   "not done and why.")
    return out


def _glm_run(p: Project, task: dict) -> bool:
    """Whether `task` is a code task whose last worker run ran on the GLM model (glmshare.on_glm)."""
    if task.get("kind") != "code" or not task.get("id"):
        return False
    from . import glmshare
    r = p.db.one("SELECT provider, model, note FROM runs WHERE task=? AND role='worker' ORDER BY id DESC LIMIT 1",
                 (task["id"],))
    return bool(r) and glmshare.on_glm(r["provider"], r["model"], r["note"])


def builtin_applies(task: dict, handoff) -> bool:
    """Whether the built-in check looks at this hand-off: a finished one (STATUSES) of a code task."""
    return task.get("kind") == "code" and isinstance(handoff, dict) and handoff.get("status") in STATUSES


def _branch_diff(p: Project, task: dict):
    """A code task's branch diff: (repo root, head, diff text, fork point), or None when git cannot tell."""
    from . import worktree
    try:
        wt = Path(p.worktrees) / f"t{task['id']}"
        root = wt if worktree.is_git(wt) else Path(p.root)
        head = task.get("branch") or ("HEAD" if root == wt else "")
        base = worktree.fork_point(root, task["id"]) or worktree.resolve_base(p)
        if not head or not base:
            return None
        got = subprocess.run(["git", "-C", str(root), "diff", "--no-color", "--no-ext-diff", "-U0", f"{base}...{head}"],
                             capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_S)
        if got.returncode != 0:
            return None
        fork = subprocess.run(["git", "-C", str(root), "merge-base", base, head], capture_output=True, text=True,
                              timeout=DEFAULT_TIMEOUT_S).stdout.strip()
        return root, head, got.stdout, fork or base
    except (OSError, RuntimeError, subprocess.SubprocessError):   # git cannot tell: the hand-off stands
        return None


def builtin_check(p: Project, task: dict, handoff) -> dict | None:
    """The built-in check's failure ({name, output}) on a code task's hand-off: glm_test_hygiene, slop.new_noisy
    and handoff_gaps on a GLM run's branch diff, else only its new tests' base-failing run line (new_test_gap).
    None when it passes, does not apply or git cannot tell."""
    if not builtin_applies(task, handoff):
        return None
    got = _branch_diff(p, task)
    if got is None:
        return None
    root, head, diff, fork = got
    if not _glm_run(p, task):   # a blocked or failed hand-off may owe no run: it could not run the test
        gap = new_test_gap(diff, handoff) if handoff.get("status") in ("done", "needs_review") else None
        return {"name": CODE_CHECK, "output": gap} if gap else None
    from . import slop
    try:
        noisy = slop.new_noisy(root, fork, head)
    except (OSError, subprocess.SubprocessError):   # git cannot tell: the rest of the check still stands
        noisy = []
    bad = glm_test_hygiene(diff)
    out = (["Tests must not touch the real machine's paths:", *bad[:20]] if bad else []) \
        + (["Comments and docstrings state the rule, never an incident (a date, id or host):",
            *(f"{n['at']}: {n['text']}" for n in noisy[:20])] if noisy else []) \
        + handoff_gaps(diff, task.get("spec") or "", handoff)
    return {"name": GLM_CHECK, "output": "\n".join(out)} if out else None


def _digest(handoff: dict) -> str:
    return hashlib.sha256(json.dumps(handoff, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _input_file(handoff: dict, run_dir: Path | None) -> Path:
    """The run's result.json when it holds `handoff` as is, else a copy of `handoff` in the run dir."""
    if run_dir is not None:
        own = run_dir / "result.json"
        try:
            if json.loads(own.read_text()) == handoff:
                return own
        except (OSError, ValueError):
            pass
        try:
            durable_write(run_dir / INPUT_FILE, json.dumps(handoff, indent=1))
            return run_dir / INPUT_FILE
        except OSError:
            pass
    fd, path = tempfile.mkstemp(prefix="ttp-handoff-", suffix=".json")
    os.close(fd)
    durable_write(Path(path), json.dumps(handoff))
    return Path(path)


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= OUTPUT_CHARS else "…" + text[-OUTPUT_CHARS:]


def _run_one(p: Project, check: dict, task: dict, path: Path,
             base_env: dict | None = None) -> tuple[int | str, str]:
    """(exit code, or "timeout" or "error: ..."; its output's tail). The command's process group is
    killed on a timeout."""
    env = dict(os.environ if base_env is None else base_env, TTP_RESULT_JSON=str(path), TTP_TASK_ID=str(task["id"]),
               TTP_TASK_KIND=str(task.get("kind") or "work"))
    if task.get("kind") == "harness":   # the daemon's own verdict, so a check never asks for a merge the run cannot make
        from . import harnesstree
        try:
            env["TTP_HARNESS_LANDING"], env["TTP_HARNESS_LANDING_WHY"] = harnesstree.handoff_landing(
                p, dict(task, result=path.read_text()))   # the hand-off checked, not the stored one
        except (OSError, subprocess.SubprocessError) as e:
            env["TTP_HARNESS_LANDING"], env["TTP_HARNESS_LANDING_WHY"] = "", f"git could not tell: {e}"
    cmd = check["run"].replace("{result}", shlex.quote(str(path)))
    from . import boundedchild
    res = boundedchild.run(cmd, check["timeout_s"], shell=True, cwd=str(p.root), env=env, text=False,
                           stderr="stdout", grace=KILL_WAIT_S)
    out = _tail(res.out.decode(errors="replace"))
    if res.timed_out:
        return "timeout", out
    if res.rc is None:
        return f"error: {res.error.removeprefix('could not start: ')}", ""
    return res.rc, out


def run(p: Project, cfg: dict, task: dict, handoff, run_dir: Path | None, deadline: float | None = None,
        base_env: dict | None = None) -> dict:
    """Runs builtin_check, then the checks that apply, in order, up to the first that fails. {"failed": {name, output} or
    None, "broken": [{name, run, why, output}]}; {} when none apply. By `deadline` (time.monotonic)
    each check's timeout is cut to the time left, and none starts after it."""
    if bad := builtin_check(p, task, handoff):
        return {"failed": bad, "broken": []}
    checks = applies(cfg, task, handoff)
    if not checks:
        return {}
    path = _input_file(handoff, run_dir)
    got: dict = {"failed": None, "broken": []}
    for c in checks:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                got["broken"].append({"name": c["name"], "run": c["run"], "why": "timeout", "output": ""})
                break
            c = dict(c, timeout_s=min(c["timeout_s"], left))
        rc, output = _run_one(p, c, task, path, base_env)
        if rc == 0:
            continue
        if rc == 1:
            got["failed"] = {"name": c["name"], "output": output}
            break
        why = rc if isinstance(rc, str) else f"exit {rc}"
        got["broken"].append({"name": c["name"], "run": c["run"], "why": why, "output": output})
    return got


def prefetch(p: Project, cfg: dict, task: dict, handoff, run_dir: Path | None) -> None:
    """Runs the checks before the transaction that records `task`'s hand-off; verdict reads it."""
    if not applies(cfg, task, handoff) and not builtin_applies(task, handoff):
        return
    now = time.time()
    for k in [k for k, (until, _) in _seen.items() if until <= now]:
        _seen.pop(k, None)
    got = run(p, cfg, task, handoff, run_dir)   # its expiry counts from the answer, however long the checks took
    _seen[(str(p.root), int(task["id"]), _digest(handoff))] = (time.time() + SEEN_TTL_S, got)


def verdict(p: Project, cfg: dict, task: dict, handoff, run_dir: Path | None) -> dict:
    """prefetch's answer for this hand-off, else the checks run now; {} when none apply."""
    if not applies(cfg, task, handoff) and not builtin_applies(task, handoff):
        return {}
    hit = _seen.pop((str(p.root), int(task["id"]), _digest(handoff)), None)
    if hit and hit[0] > time.time():
        return hit[1]
    return run(p, cfg, task, handoff, run_dir)


def stop_sendbacks(run_dir: Path | None) -> int:
    """The stops the Stop hook refused in this run on a failed check (at_stop)."""
    try:
        return max(0, int((run_dir / STOP_FILE).read_text())) if run_dir is not None else 0
    except (OSError, ValueError):
        return 0


def at_stop(run_dir: Path) -> str | None:
    """In the worker's Stop hook: the reason to refuse the stop when a check fails (exit 1) on the run's
    result.json and the send-back cap leaves room, else None. The refusal is counted (STOP_FILE) before
    it is returned: a count that cannot be written never refuses. Raises on faults; the hook fails open."""
    try:
        used, cap = (int(x) for x in os.environ.get(SENDBACKS_ENV, "").split("/"))
    except ValueError:
        return None
    stops = stop_sendbacks(run_dir)
    if used + stops >= cap:
        return None
    handoff = json.loads((run_dir / "result.json").read_text())
    if not isinstance(handoff, dict) or handoff.get("status") not in STATUSES:
        return None
    p = Project(os.environ["TTP_PROJECT"])
    if not p.exists():   # never a database made where no project is
        return None
    cfg = p.config()
    task = p.db.task(int(os.environ["TTP_TASK"]))
    if not task or not (applies(cfg, task, handoff) or builtin_applies(task, handoff)):
        return None
    env = {k: v for k, v in os.environ.items() if k not in RUN_VARS}
    env[AT_ENV] = "stop"
    bad = run(p, cfg, task, handoff, run_dir, time.monotonic() + STOP_BUDGET_S, env).get("failed")
    if not bad:
        return None
    durable_write(run_dir / STOP_FILE, str(stops + 1))
    out = bad["output"] or "(no output)"
    return (f"tt-project: the project's hand-off check '{bad['name']}' rejected your hand-off (send-back "
            f"{used + stops + 1} of {cap}); nothing was posted or queued. Fix what it names, write "
            f"$TTP_RUN_DIR/result.json again, then stop. Its output:\n{out}")


def reason(failed: dict, n: int, cap: int) -> str:
    """What the sent-back run is told: the check, its output and how to pass it."""
    out = failed["output"] or "(no output)"
    return (f"the project's hand-off check '{failed['name']}' rejected your hand-off (send-back {n} of {cap}); "
            f"nothing was posted or queued. Fix what it names and write result.json again. Its output:\n{out}")


def event_tail(output: str) -> str:
    """A check's output as a coordinator event shows it: its last EVENT_OUTPUT_CHARS."""
    output = (output or "").strip() or "(none)"
    return output if len(output) <= EVENT_OUTPUT_CHARS else "…" + output[-EVENT_OUTPUT_CHARS:]


def broken_fingerprint(b: dict) -> str:
    sig = f"{b['name']}\0{b['run']}\0{b['why']}"
    return f"{BROKEN_EVENT}:{hashlib.sha256(sig.encode()).hexdigest()[:12]}"


def broken_text(task: dict, b: dict) -> str:
    return (f"The project's hand-off check '{b['name']}' is broken ({b['why']}; only exit 0 = pass and 1 = send "
            f"back are valid), first on #{task['id']} {task['title']}: hand-offs are accepted without it until it "
            f"is fixed (worker.handoff_checks). Command: {b['run'][:300]}. Its output:\n{event_tail(b['output'])}")
