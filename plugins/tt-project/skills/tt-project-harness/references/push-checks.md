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

## Extra commands behind a wrapper

`checks.wrap_cmd` (project setting, default unset) is a project's own lock or queue command that each command given after `ttp checks --` runs behind, for a project that serializes a shared host's test suite with its own script.
Set, an extra command runs as the wrapper's words followed by `bash -e -o pipefail -c '<command>'` as separate arguments, so quoting and spaces survive; the wrapper runs outside the process sandbox, and its exit code and output pass through unchanged.
A wrapper (a path relative to the checkout, or a name on PATH) that is missing or not executable fails the checks with a line naming the key; the command never runs unwrapped. `ttp push --own` runs the recorded extras behind it too.
`delivery.push_checks` never go through it (a check that needs the lock names the wrapper in its own command), nor do extras run on a check host.
It is not chat-settable: the user sets it with `ttp config <project> checks.wrap_cmd '<command>'`.

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

`ttp checks` and push checks share a host-wide slot cap (`checks.host_max`, account-level: read it with `ttp config --account checks.host_max`, set it with `ttp config --account checks.host_max N`), waited for by project in turn, then task priority, then arrival; a required owner of an open obligation goes first in its project.
Checks gating a push to the push branch (push-queue batches, `ttp push`) skip that queue: each push branch has one priority slot beyond the cap, host-wide, so approved changes never wait behind task checks.
Task checks keep their cap and order, except that priority-1 task checks (live-impact work) and obligation owners go ahead of other projects' waiters for at most `checks.priority_turns` (default 3) turns in a row while others wait.
`ttp push --own` queues like them.
Test workers: every check run then takes up to `checks.test_run_max` slots (default min(8, half the pool)) of a host pool of `checks.test_pool` (default half the cores, at least 2; 0 turns it off), shared by all projects, so concurrent suites never start a worker per core each; a project may only lower both.
The grant is set as `PYTEST_XDIST_AUTO_NUM_WORKERS` and `TTP_TEST_SLOTS`, a pytest `-n N` above it is lowered to it (with a line saying so), and checks run under `nice -n 10` and best-effort lowest `ionice` where present.
Builds (make, ninja, `cmake --build`) use a separate host build pool of the same default size (`checks.build_pool`, `checks.build_run_max`), so a long build never starves test checks: workers get MAKEFLAGS=-j<k> and CMAKE_BUILD_PARALLEL_LEVEL=<k>, and `ttp clip` of a build holds k build slots and lowers an explicit -j above k (a worker's own build outside `ttp clip` with -j above k, an unlimited `make -j` or a ninja with no -j is refused); a build inside a check uses the check's test grant.
One project holds at most `checks.test_project_max` test slots over all its runs (default twice a run's share), waiters get slots first-come, push checks take their branch's reserved test slot when the pool is full, and a project runs at most `checks.slots` full `ttp checks` per git repository at a time (chat-settable whole number; default the runs the host's test-slot pool fits side by side, at least 2 and at most 4, lowered only by little available memory; each run has its own clean checkout, TMPDIR, output folder and port block `TTP_PORT_BASE`/`TTP_PORT_COUNT`, and checks in the authoring worktree still run one at a time per worktree): the most urgent task priority goes first (then arrival; a long waiter ages up, so none starves), and a run past its wall-time cap (`checks.wall_cap_s`; default 3x the longest recent run, within 1-6 h) is stopped, frees the slot and is recorded as a timeout, never a pass.
A run whose commands go behind `checks.wrap_cmd` takes that wrapper lock's own lane instead of a repository lane: runs behind one wrapper lock go one at a time, and lock-free checks (lint, other trees' tests, harness checks) run in their repository's lanes beside it rather than waiting behind it. Any wrapper gets this lane, even one that fails instead of waiting when its lock is busy (`flock -n`, `flock -w 60`): runs behind it never go side by side, so they never fail each other's lock; prefer a blocking wrapper (`flock <file>`), since a non-blocking one still fails when anything else holds the lock. At most `checks.test_project_max` of a project's checks run at once over all its lanes (project, then account setting; chat-settable, a whole number of at least 1; unset, the project's test-worker budget). Waiting checks start in task-priority order (priority 1 first, then age) across the project's lanes, and a priority-1 check whose lanes are held only by less urgent runs starts beside them in an extra lane while the host's load is under half its cores, never beside a run in its own worktree.
Light checks never wait for a lane: a run of only syntax, lint, format or validation commands (`python -m py_compile`, `ruff check`, `git diff --check`, ...; plus the words of `checks.light_cmds`, or `ttp checks --light`) runs at once and still fails when it fails; a command with anything heavier in it (`ruff check . && pytest`, `$CMD`) is not light.
A lane with a dead or silent holder is reclaimed and logged; a waiter past `checks.max_wait_s` (default 300, 0 off) that is cheaper than every holder takes an extra lane; a wait past `checks.wait_event_s` (default 900, 0 off) raises one `checks_slot_wait` event. `ttp checks status` (`--json`) shows holders and waiters read-only, each lane named by its group (a repository or a wrapper lock).
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

`delivery.checks_spill_hosts: ["check-host-a", "check-host-b"]` (ordered ssh aliases; unset or empty keeps every check here) lets a `ttp checks` run that has waited `delivery.checks_spill_after_s` (default 60) for a slot here (lane, host-wide checks slot, test slots) run on the first listed host that is allowed, reachable and has a free slot, the same way as a check host. Allowed means named in the charter, in the machines list (not retired), not paused, and its machine lock (`ttp lock <alias>`) not held or reserved by anyone else; the run holds that lock shared while it runs (exclusively on a device lock), so an exclusive holder keeps spilled checks off. A host that is not allowed, whose lock is busy, unreachable, missing git or tar, or full (its lanes from cores and memory, less the `ttp-check.*` folders running there and this machine's claims) is skipped with the reason, never a failure, and the run keeps its place here and tries again after the same bound. Only runs whose every check would run here, unwrapped (no `host`, no `checks_host`, no `"cwd": "authoring"`, no `checks.wrap_cmd`) spill; light runs never wait. A run that does not pass on a spill host (including an ssh fault there, or the wall-time cap, which records no task timeout) runs again here without spilling, and only that result counts. `checks.json` records `spilled_to` and `hosts`; `ttp status` and `ttp checks status` show runs on a spill host. A list is required: `ttp config` and the config check refuse a single string.
