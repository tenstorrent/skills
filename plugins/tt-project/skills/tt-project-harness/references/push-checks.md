# Push checks

## Which tree runs

A pass is recorded for the commit's tree, so the checks run on exactly that tree.
`ttp checks` runs each local check in a fresh detached checkout of HEAD (`git worktree add --detach` in the system's temporary folder), never in the authoring worktree: an ignored, untracked or uncommitted file there cannot make a check pass. The checkout is removed after a pass, a failure, a timeout or a stop; one a killed run left is removed by the next `ttp checks`.
Uncommitted changes and untracked files are named as not checked, with the commit that was.
Submodules come from the authoring worktree's checked-out copies, never the network; a gitlink commit that copy lacks fails the checks with a clear message, never a run in the authoring tree.
A failure lists up to five files that exist only in the authoring worktree and the check may have needed ("present only in the authoring worktree, not committed: ...").
`{"run": "...", "cwd": "authoring"}` runs one check in the authoring worktree (it needs build outputs made there); its pass is labelled as not from a clean tree in the output and in `checks.json` (`authoring_tree`). The default is `"clean"`.
`ttp push`, `ttp land` and the push queue already run their checks in scratch worktrees of the exact commit they push, and remote checks copy it with `git archive` (below).

## Which list runs

Each repo runs its own list. `delivery.push_checks_by_repo` maps `"harness"` (the project's own harness repo), a repo path (absolute, `~`, or relative to the project root) or a main checkout's folder name to a list in the `push_checks` form; `delivery.push_checks` covers every repo the map does not name.
The harness repo never inherits the code repo's `push_checks`. With no `"harness"` entry, `ttp checks` there says `no repo checks configured for the harness repo` and runs the built-in harness default (a whitespace check, and pytest of `runtime/tests` and `bin/tests` where they exist); `--purpose harness` runs only `push_checks`' purpose harness entries, else that default.
That pass is the harness repo's own, never a pass of the code checks; `checks.json` names the list's source in `checks_from`.

## Fast path

Opt-in, off by default: with no `delivery.checks_fast_globs`, every diff runs the full checks.
A project that lists globs there gets it: a diff whose paths all match them (never code, tests, templates, skills: `delivery.checks_fast_exclude`) runs only the whitespace and CRLF checks; the pass is not a full pass and never counts for a PR or `on_pass`.
`ttp checks --full` runs everything, also when queued or relaunched; a recorded full pass is reused.
Known gap: the project owns its globs; one covering installed or test-asserted content (such as a plugin README that tests read) skips the checks those changes need.

## Pre-stage before detached checks

On by default. Where `delivery.check_prestage` is unset and the project's checks run pytest here, `ttp checks --detach` derives one: up to 10 tests that failed in the most distinct detached checks runs over 14 days, leaving out any recorded at 5 s or more. A derived pre-stage that times out records the test it cut off as slow (and, if none had started, every test in its list), so the next one leaves it out instead of timing out again. It runs only their files with a name filter, so a test since renamed or removed is deselected (pytest exit 5 passes), with the pytest check's venv, `env` and sandbox setting; pytest exits 3 and 4 never hold the checks back. With no such history none runs. The daily audit reports the list and the failures it caught. `''` or `false` turns the pre-stage off.
An explicit `delivery.check_prestage` is used unchanged: a short command (a string, or `{"run": "...", "timeout_s": 60}`; default 90 s, at most 900) that `ttp checks --detach` runs first, in the foreground, at the worktree's top. The tool call that runs `ttp checks --detach` needs a timeout above it; a signal that ends that command (SIGTERM, SIGHUP, Ctrl-C) also stops the pre-stage.
Pick fast tests that often catch mistakes (static invariants, lint), so a worker sees their failure in the same run instead of after the whole detached suite and a later wake.
It fails: its output's tail is printed, nothing starts in the background and the command exits 1. It passes or times out: the full checks start as usual. A command the host lacks (exit 126/127) or a malformed setting is reported and skipped; `--no-prestage` skips it for one start.
It only adds an early run: the full checks still run every command, and it never marks a check as passed. Foreground `ttp checks`, `--purpose harness` and the harness repo do not run it. It is skipped, with a line saying why, where the detached checks would run nothing new: a recorded pass of the same checks on this tree (`--fresh` runs it) or a fast-path docs/data diff (`--full` runs it).

## Priority

`ttp checks` and push checks share a host-wide slot cap (`checks.host_max`), waited for by project in turn, then task priority, then arrival; a required owner of an open obligation goes first in its project.
Checks gating a push to the push branch (push-queue batches, `ttp push`) skip that queue: each push branch has one priority slot beyond the cap, host-wide, so approved changes never wait behind task checks.
Task checks keep their cap and order, except that priority-1 task checks (live-impact work) and obligation owners go ahead of other projects' waiters for at most `checks.priority_turns` (default 3) turns in a row while others wait.
`ttp push --own` queues like them.
Test workers: every check run then takes up to `checks.test_run_max` slots (default min(8, half the pool)) of a host pool of `checks.test_pool` (default half the cores, at least 2; 0 turns it off), shared by all projects, so concurrent suites never start a worker per core each; a project may only lower both.
The grant is set as `PYTEST_XDIST_AUTO_NUM_WORKERS` and `TTP_TEST_SLOTS`, a pytest `-n N` above it is lowered to it (with a line saying so), and checks run under `nice -n 10` and best-effort lowest `ionice` where present.
Builds (make, ninja, `cmake --build`) use a separate host build pool of the same default size (`checks.build_pool`, `checks.build_run_max`), so a long build never starves test checks: workers get MAKEFLAGS=-j<k> and CMAKE_BUILD_PARALLEL_LEVEL=<k>, and `ttp clip` of a build holds k build slots and lowers an explicit -j above k; a build inside a check uses the check's test grant.
One project holds at most `checks.test_project_max` test slots over all its runs (default twice a run's share), waiters get slots first-come, push checks take their branch's reserved test slot when the pool is full, and one full `ttp checks` runs per project at a time.
The batch log and `ttp status` show `push priority slot: <holder>`; slots are OS file locks, so a killed holder frees its slot.

## Outputs

`delivery.push_checks` receive a writable absolute `TTP_RUN_DIR`, never empty or `/`.
Foreground `ttp checks` uses the worker's run directory; detached checks, detached pushes and push-queue batches get unique directories under project `state/check-runs/`, outside the checked worktree.
Use `"$TTP_RUN_DIR/report.json"` for durable outputs.
`checks.pid` and queue markers record `check_run_dir`; a restarted daemon rebuilds queued directories and a rebooted check invocation gets a fresh one.
Outputs survive worker cleanup and completion; only detached checks' temporary scratch is reclaimed once its lifetime lock is free.
Symlinked directories or locks are refused.
Missing, invalid or unwritable output storage fails before checks; never skip a check or substitute `/`.
Detached launches strip `TTP_PIDNS` and `TTP_CGROUP`.

## Checks on another host

`delivery.checks_host: "test-box"` (an ssh alias or machines-list alias) runs every check there; a check object's own `"host"` overrides it (`"local"` keeps that check here) and its `"env"` exports variables first, e.g. `{"run": "pytest -q", "host": "build-host", "env": {"PATH": "$HOME/.local/bin:$PATH"}}`. The exact head is copied with `git archive` over ssh into a fresh temporary directory, made a git checkout (one commit), and the command's output and exit code come back; the directory is always removed. ssh failing or a host without git or tar is an environment fault (`env:check-host:<host>`): the check waits (exit 75), never counts as failed or passed. `ttp checks` records and log lines name the host.
