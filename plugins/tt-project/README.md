# tt-project

Long-running, self-driving projects that run on your own machines.

Tell your agent "start a tt-project called `docs-refresh` that keeps our README accurate", and a
project comes up with its own coordinator. The coordinator plans the work and hands it to
headless workers in isolated workspaces. It watches pull requests and logs, fixes what it finds,
remembers what you tell it, and stays inside a budget. Close the chat and it keeps going. Open any
chat later, in Claude Code, Codex or Cursor, and connect to it by name.

Nothing runs in a hosted cloud. The project runs on this machine or on an always-on box you
name, which is also where it can use local hardware.

## Install

```text
/plugin install tt-project@tenstorrent-skills      # Claude Code, after adding the marketplace
codex plugin add tt-project@tenstorrent-skills     # Codex
```

Cursor: link the plugin folder into `~/.cursor/plugins/local/tt-project`, or start the CLI with
`agent --plugin-dir <this folder>`.

The first use installs a `ttp` command for your user (`ttp setup`). Python 3.9+ and `git` are the
only requirements; the runtime uses the standard library.

## Using it

| You say | What happens |
|---|---|
| "Start a tt-project called X on box B: <brief>" | the project is created on B and starts working |
| "Connect to project X" | this chat attaches; replies and alerts arrive here |
| anything addressed to the project | relayed to the coordinator; its answer comes back to this chat |
| "What is X doing?" | `ttp status X`: running work, blockers, spend, why idle |
| "How are all my projects?" | `ttp overview` (or the web app's Projects tab): one line per project on this machine |

The brief can be inline text, a file, or links. Goals, restrictions ("never access the internet")
and preferences you add later become part of the project's charter and memory.

## How it works

```
 chats (any host) ─┐                ┌─ worker: headless agent in its own worktree ─┐
 web app ──────────┼─ inbox ─► daemon ─► coordinator turn (decides, never works)   ├─► results
 watchers ─────────┘   (deterministic)  └─ worker … (parallel, budgeted, stall-guarded)┘
```

- **Daemon** (one per project): schedules, watchers, budget gates, message routing, web app.
  Deterministic and cheap. It starts a model only through a run.
- **Coordinator**: a short, tool-less decision over a digest of the project, batched and rate
  capped. Anything needing files, commands or deep thought becomes a task.
- **Workers**: one task each, in a git worktree on their own branch for code, with a dollar
  budget, a wall clock, and a stall guard. Each hands off a structured result. A worker whose
  machine or queue is busy hands the task back to retry later, instead of waiting inside the run.
  The wall clock and stall guard count only time the host is awake. A run a host sleep cut short
  is retried without spending an attempt and is not counted as waste. After a wake, nothing new
  starts until the host has been awake for `budget.wake_settle_s` (default 300 s), so a laptop's
  brief maintenance wakes start nothing. Messages from people are still answered at once.
  A run a reboot, a sleep or a lost supervisor cut short after real progress
  (`budget.resume_lost`, default $0.50 or 10 min) continues its agent session in the same
  working directory with a short prompt, instead of starting over.
  When the coordinator rescopes a running task, the change reaches the worker mid-run.
- **Memory and charter**: plain files in the project's harness, one fact per file. A temporary entry carries its end (a time, a plain-language condition or a probe); the daemon retires it to the archive or `CHARTER.history.md` once that passes and says so.
- **Watchers**: pull requests (CI, reviews, mergeability) and logs, reporting only changes.
  With Jev enabled, new observations are screened by a cheap decision model first.

## Budget

- Subscription plans: a plan's capacity is lost at each reset, so the project uses it. Below the
  line (90%, `100 - budget.reserve_pct`) it runs all its parallel workers; it does not spread use
  evenly over a window. Running work keeps using the plan after it starts, so near the line it
  estimates what each worker will still add (the account's measured burn per worker, times how
  long a run usually lasts here) and runs only as many workers as fit under the line: fewer in the
  last stretch, none new while those are all busy or the running ones alone would reach it. At the
  line nothing new starts until the window resets; the rest stays yours. Running work is never
  stopped.
- In that last stretch deep tasks run at the standard tier. `ttp status` and the web app say when
  the line holds new starts back. You get an alert only when the project reaches the line, once
  per window; it clears by itself when the window resets.
- Usage-billed accounts: $100 per 24 hours and $200 per 7 days per project by default. A new run
  starts only if its budget fits in what is left of both caps. A plan account whose successful
  runs stop reporting plan windows falls under these caps too (failed or silent runs do not count).
  The caps count only spend made on an account that was billed by use at the time: after a switch
  from a plan account to a usage-billed one, the plan's earlier spend stays out.
- Global daily cap (usage-billed, $200/day by default): `budget.global_daily_usd` stops new work in
  every usage-billed project on the account once the whole account's tt-project spend today reaches
  it. "Today" is the rolling 24 hours unless `budget.day_start` is set. Change it for every project
  on the machine with `ttp config --account budget.global_daily_usd 500` (or for one project with
  `ttp config <name> budget.global_daily_usd 500`); 0 turns it off. Plan accounts are bounded by their
  windows instead and never meet it. Running work and replies to your messages go on; it clears by itself at the day's reset
  (or, without a budget day, as spend leaves the last 24 h). Only spend on the same provider and the same account counts (an account is compared by a
  hash, so its name never leaves the machine); rows with no account recorded count, to be safe.
  The total covers every project `ttp list` shows on this machine, the machines of the registry's
  projects elsewhere, and machines tagged `tt-project` in `ttp machines` (`ttp machines add <alias>
  --tags tt-project`). Other machines are asked over ssh (BatchMode, no prompts) with
  `ttp spend-today`, at most every 10 minutes; their answers are cached in
  `~/.tt-project/global-spend.json`. A machine not heard from for 30 minutes still counts with its
  last answer for the same day and is shown as stale. A machine that can reach another but not the
  other way round (a laptop that can reach a server, but not the server the laptop) pushes its own
  `spend-today` there instead: with the global cap on, each machine sends it over ssh to
  `ttp spend-today --receive` on every machine it asks, every 10 minutes (every 6 hours to a machine
  that says it already asks this one; less often after failures), in the background. Pushed answers
  are kept in `~/.tt-project/global-spend-pushed.json` and count like asked ones, stale after 30
  minutes the same way; a machine both asked and pushing counts once, with its newer answer.
  Each answer, asked or pushed, also carries that machine's estimate of its other Claude Code
  sessions (`other_sessions`: provider, account hash, dollars, `estimated: true`; `null` while its
  logs are not read yet). It counts with that machine's answer, so once per machine, and the total
  names it apart from this machine's own sessions. An answer from an older tt-project has no such
  field: it still counts, its sessions count 0, and the total says they are missing.
  `budget.push_spend_to` (account-level) names the machines to push to instead, or `"none"` turns
  pushing off. If the total cannot be worked out at all, the
  per-project caps stay in charge. Web, desktop and cloud sessions are not seen; another
  source can be added in code with `globalcap.add_other_source(fn)`, where
  `fn(provider, account, start, end)` returns `(usd, label)`.
- Budget day: with `budget.day_start` set (`"HH:MM"`), "today" is a fixed day starting at that time
  in `budget.timezone` (an IANA name such as `Europe/Berlin`, default `UTC`; the host's own zone
  plays no part), 23 or 25 hours long across a daylight-saving change. The daily cap and the global
  cap count that day. Empty (the default) keeps the rolling 24 hours. Weekly caps stay rolling.
- These keys (and `budget.push_spend_to`) are account-level: `ttp config --account KEY VALUE` writes them to
  `~/.tt-project/settings.json` (mode 0600), which every project on the machine reads under its own
  `project.json` (a project may still override a key, except `budget.push_spend_to`, which is read
  from the account settings only). `ttp config --account KEY` reads one; an
  empty value removes it. `ttp spend-today [--json]` prints this machine's tt-project spend for the
  budget day by provider and account hash, and its other Claude Code sessions as estimated; it is
  what other machines ask for.
- Work backs off in steps as spend rises, pauses at the cap, and tells you how to raise it.
- A runaway guard pauses a project whose hourly spend jumps far above its own norm.
- A review runs light when the diff it checks touches no `review.risky_paths` glob and no code file
  with a risky name (`review.risky_names`: state, database, schema, migration, push, budget, billing,
  spend, spend cap, release and upgrade files by default; `[]` turns it off), and is docs and tests
  only or small (`review.light_max_lines` non-doc lines, default 80); standard otherwise. Every re-review after a
  failed review and every retry runs standard; only the coordinator picks deep. An optional
  `review.light_paths` glob list narrows light further: every non-doc file must match one of its
  globs (unset or empty: any path may go light). The review run's note records the pick and the
  rule behind it (`review_tier`).
- When delivery has a review step (`delivery.review_before_pr` or a `delivery.push_branch`), the
  daemon queues the review of each finished code task whose hand-off has no follow-ups, notes or
  findings itself (`review.auto`, on by default), and that hand-off starts no coordinator turn.
  Any other hand-off leaves the review to the coordinator's turn. `review.auto_notes` is added
  to each such review's spec (for example, what to do after a push). When the task's PR already
  carries the reviewed head (as pr-watch last read it, or as the task's own `ttp push --own`
  pushed it), the work is delivered: the review is review only, with no push to
  `delivery.push_branch` and no push-queue approval. So is the review of a change that must not
  reach the push branch: every file its diff since the base changes matches
  `delivery.push_exclude_paths`, its hand-off sets `no_push` (true or the reason), or its spec has
  a line starting `no_push:` (the reason follows). Prose is never read as a ban: free text cannot
  tell a ban from a sentence that reports, quotes or conditions one. The review's spec says why, its fix and re-review
  stay review only, and the push queue ignores its approval. Every review is review only while
  `delivery.push_branch` can never be pushed to: it names main or master, which `ttp push` always
  refuses, or the code repo has no git remote (or not the one it names). The config check and
  `ttp doctor` say so. A review the coordinator adds
  for the same work replaces the daemon's while it has not started. A review that fails with
  follow-ups gets one fix task on the reviewed branch and a re-review from the daemon, and the
  tasks waiting on the failed review wait on the re-review instead of being blocked (at most two
  rounds per stack; a failure without fix follow-ups still blocks them, and `upstream:` notes and
  deferred follow-ups stay with the coordinator). Across stacks, once `review.area_fail_cap` (3; 0
  off) reviews of one area failed within 48 h (a lineage linked by `continues`, follow-ups,
  `depends_on` and `Review #N` / `Fix review #N` titles, or changes with the same main file), the
  daemon queues no more fixes there and raises a high-effort coordinator turn to re-plan it
  ("repeated review failures in one area", also counted in the daily review).
- A review's result: `done` lets the change proceed (its PR, push or push-queue approval and its
  dependents), `failed` stops it and starts the fix flow above. A review handed off `done` with
  `metrics.verdict` `changes_needed` (or `changes_requested`, `rejected`) counts as failed too, so a
  project may word the rule that way. The rule sits in its own block of `prompts/kind-review.md`,
  between `<!-- ttp:result-rule -->` marker lines the prompt leaves out: a project that rewords it
  keeps its wording through template upgrades without merge conflicts.
- Each tier maps to a model and effort per provider (`providers.<name>.tiers`). The coordinator
  runs at `coordinator.tier` (light) unless `coordinator.model` or `coordinator.effort` is set:
  those pin the coordinator alone, so moving the light tier to a cheaper model does not move it.
  Empty (the default) follows the tier. The model names one of `core_provider`'s models.
  A tricky or blocking turn runs at least at `coordinator.unblock_effort` (high; empty turns this
  off), so it looks harder for a non-disruptive way forward. Its triggers, kept in one table
  (`coordinator.effort_triggers`) and recorded per turn in the run's note: a user message (and a
  change of plan in it); a blocked, failed or needs-review task, a failed review, a dead dependency,
  an expired or broken deferral or a stalled review; a task failing `coordinator.repeat_fails_24h`
  (2) times in 24 h; a task's external waits (not its own checks, jobs, push or planned window)
  changing reason, going on for 24 h, or reaching `coordinator.repeat_waits_24h` (8) in 24 h, where
  a wait on a live `ttp detach` job or an unpaused `ttp lock` resource counts only past
  `coordinator.live_waits_max` (6) waits on the same ones in 24 h or once the job's log stops growing; a
  rejected action; free worker slots while every queued task is held by a dependency or a paused
  resource or deferred on purpose (`start_after`, `start_when`), at least one of them held (planned
  deferrals alone are routine); a high or critical event or alert; resource trouble (not waits only); a costly or irreversible step (an ask timing out, a task's budget spent, PR findings
  or a clean PR, a failed push, the budget gate entering or leaving red); an idle wake finding
  blocked tasks or open asks. Routine bookkeeping stays at the base effort; such a turn that finds
  its batch harder than it looked returns `escalate`, and the same batch reruns once at high
  effort (never twice, and never from a raised turn; kv `escalations` counts them). A pinned
  `coordinator.effort` wins over all of this. `coordinator.effort_skip_triggers` (none by default)
  lists trigger labels, as recorded in the note, that never raise a turn. With a Jev key, Jev also rates a turn the triggers
  leave below high 'routine' or 'needs thought', but only when it has a new failed or blocked task,
  high or critical event, user message, or an external wait older than `coordinator.jev_wait_h` (6);
  other turns skip the call and say so in their note. A reason rated at `coordinator.jev_threshold`
  (0.7) or above raises the turn the same way, and the same events never raise a second turn. Each
  call is logged with the effort it led to and what the turn did, and reported in the daily review;
  `jev.uses.coord_effort` = `off` turns the check off.
- The web app shows spend per day, per task and per recurring job, and plan-window peaks for the
  last two weeks.
- The web app's header and `ttp status` show the budget in one line, for example
  `5h 4% - resets in 3.9 h, 7d 21% - resets in 6.0 d, 24h $0.17 virtual, 5h avg 31%, 7d avg 72%`.
  The windows and averages are the account's (an average is the mean of each completed window's
  peak: 5-hour windows over 7 days, weekly ones over 3 weeks). The dollars are this project's last
  24 h: `virtual` (list-price equivalent) on a plan, `actual` when billed by use (only spend made
  on a usage-billed account). A usage-billed account with a budget day or a global cap shows
  `today $1.20 this project, $85 of $1000 global - resets in 6.5 h` instead, with
  `(1 machine stale)` when a machine's answer is old.

## Parallel work

- Up to 6 workers per project run side by side (`budget.max_parallel_workers`); on a plan, all of
  them until the last stretch before the line. When a plan is below its line, slots sit idle and
  nothing is queued, the coordinator is asked for more work, less often each time it finds none.
- Workers, reviewers, everything they start (detached checks, tests, reference models) and the
  daemon's pushes run at low CPU priority, 10 nice levels below the daemon (`runner.nice`, 0-19;
  0 = normal priority), so they never slow the machine's own work. The daemon and the coordinator
  keep normal priority. Each run's `exit.json` records the level its agent ran at.
- On macOS, while the project has work (a running run or push, or queued work that can start now)
  and the machine is on AC power, the daemon keeps it from idle-sleeping with `caffeinate -i`, tied
  to the daemon's process so it ends with it. A closed lid or a sleep you ask for still sleeps the
  machine. It lets go when the work ends, on battery, or with `runner.prevent_idle_sleep` set to
  `off` (`auto`, the default, is on for a Mac; other platforms never hold one). `ttp status` and
  the web app say when it is held, or why not while there is work.
- A shared device or machine is taken per command, through its own queue (for example a device
  broker) or `ttp lock <resource> -- <command>`, so the rest of each task runs in parallel. A task
  marked exclusive holds the resource's lock for its whole run; while it waits for a slot, new
  `ttp lock` commands wait behind it, so it is not starved. `ttp lock` waiters take turns in arrival
  order; a nested `ttp lock` of a resource the command already holds passes straight through; inside
  a run a wait is capped at half the run's stall limit (not in a `ttp detach` job or a background
  driver that outlives the run), then exits 75 so the task can wait on `ttp lock --probe <resource>`
  (0 free, 75 busy or paused) without a model. Config `device.locks` (opt-in) names one device by several names: all share the first name's lock and pause, tasks that use one
  get the `needs_device` label, and at most `device.max_tasks` of them run at once. A worker starts
  a long job with `ttp detach <name> -- <command>`; a run that ends without a hand-off, or waiting
  without a `retry_when`, then waits on `ttp detach --check`, which exits 0 once each job wrote its
  exit code or its process is gone. `ttp detach --remote <ssh alias> [--dir <dir>] <name> -- <command>`
  starts the driver on another host with setsid nohup (default folder `~/.ttp-detach/<run>`, with its
  .log, .rc, .pid, .boot and .start); its probe `ttp detach --check --host <alias> <dir>/<name>` exits
  0 once the .rc exists or the driver is gone (pid dead, or that host's boot id changed), 1 while it
  runs and 255 while ssh fails. Config `device.runners.<name>` (opt-in) sets up one serial
  device-job runner per device host instead of a driver per task: `host` (ssh alias; empty = this
  machine), `dir` (its state folder there), `health` (a command that must pass before each job),
  `drop_check` (a command that tells a device drop from a plain failure), `max_drops` (default 2),
  `health_wait_s`, `job_timeout_s`, `reservation_cap_s` and `idle_exit_s`. With `reservation_cap_s`
  (the box's longest allowed device reservation) submit refuses a job whose limit is above it, a job
  queued before the cap was lowered is failed without being started, and a job with no limit of its
  own runs under the cap. `ttp devq` installs the
  runner on the host under a temporary name and renames it into place, never over a running copy; a
  runner stopped with TERM ends its health or drop check with it (jobs keep running and the next
  runner adopts them). A task queues each device job with
  `ttp devq submit <runner> --id <id> [--config <key>] [--timeout <s>] -- <command>` and waits on
  `ttp devq probe <runner> <id>`, which exits 0 once the job's done marker is written (done, failed
  or skipped, with exit code and log) or once no runner is alive while the job waits. The runner is
  started under a lock (a second start does nothing), runs jobs in arrival order, runs a job again
  after a drop or a host reboot, and skips a config that dropped `max_drops` times in a row until
  `ttp devq clear <runner> <config>`; drops go to its `drops.log`. `ttp devq status` shows the queue.
  Before it queues a job, `ttp devq submit` lints its command so a job that would fail at once never
  takes a slot: it refuses a command `bash -n` rejects and a script the command calls that is missing
  on the runner's host or not valid shell there (checked over ssh in batch mode, read-only; an
  unreachable host only warns), and warns when a script path, the job id or the workdir names a task
  other than the submitting one. `--no-lint` skips the check.
  An opt-in job guard, run by the host at submit, keeps a job from harming the host (each part is off
  until set; `--no-lint` does not skip it): `script_lint` (`"warn"` or `"refuse"`) checks the command
  and the shell scripts it calls for process-group isolation (`setsid`, plus traps on EXIT, TERM and
  INT that kill the process group) and for a `df` or `du` guard before downloads or cache writes;
  `netfs_prefixes` (network-filesystem mount prefixes, none by default) flags weight and cache paths
  that resolve onto one, symlinks followed without touching anything under the prefixes
  (`netfs_policy` `"warn"` or `"refuse"`); `disk_max_pct` refuses a submit, and holds queued jobs
  like a failing health check, while a disk in `disk_paths` (default `/` and the runner's folder) is
  fuller. A job may declare `--input <path>` (repeatable; must be readable on the host) and
  `--cold-start <s>` (a warning when it is longer than the job's limit).
  `legacy_driver` (a regex for the command line of the project's old per-task drivers) closes the
  race between the two paths: while one of the user's processes matches it, the runner does not
  start or start a job, and the probe keeps a task with a pending job waiting until it ends.
  Config `runner.device_timeout_max_s` (opt-in) caps device-job timeouts: `ttp devq submit` refuses a
  longer `--timeout` and gives a job without one the ceiling when the runner's `job_timeout_s` is
  unset or above it; each job sees its limit (also under a runner's `reservation_cap_s`) as
  `TTP_DEVQ_TIMEOUT_S`, and workers are told to keep
  every device timeout (broker jobs, driver steps) within the ceiling and split longer runs.
  Locks and pauses are per project; a
  resource that several projects on one machine use is declared shared (`shared_resources` in the
  config, or `ttp machines add <alias> --shared [names]`), and then all of them take turns on its
  slots, a pause of it holds in each, and `ttp status` shows which project holds or paused it.
  Each project still gives its own slot count (`resources`); when they differ, every project
  uses the smallest, and `ttp status` and the coordinator say so. A count lowered while a holder
  keeps a higher slot applies to the next holders. A pause outlives the resource leaving the
  share: `ttp machines` refuses to unshare or remove a paused one, and a project that drops it
  from `shared_resources` keeps the pause as its own.
- Each task edits only its own worktree. A code task branches from the first of:
  `delivery.base_ref`; `delivery.push_branch`, then a branch the charter names as
  `branch <name>`, each only if it exists here or on origin; the remote's default branch
  (`origin/HEAD`); the checked-out branch.
  Other kinds run in the project root unless `worktree.kinds` lists them (say `["work"]`); a run
  that leaves the root on another branch or with new uncommitted changes raises an alert naming it.
- Where reviewed changes go straight to a shared branch, `ttp push` publishes them guarded: it
  refuses uncommitted changes, rebases onto the latest tip, runs `delivery.push_checks` on the
  exact commit it pushes, starts over if the branch moved meanwhile, and never forces. A
  checked-out branch the remote already has (published with `ttp push --own`) is never rewritten:
  the rebase runs on a detached copy and the branch stays as it was. It exits 4
  when the rebased change edits a plugin under `plugins/<name>/` but a manifest keeps the version
  already on the branch (two batches that bumped to the same version). The target
  is `delivery.push_branch`, which must be set explicitly; it refuses without one, without
  checks unless the change touches only docs (`*.md`, `*.rst`, `docs/`, ...), when
  `delivery.push_allowed` is false, and for `HEAD`, `main`, `master` or the
  remote's default branch. Pushes to one branch take turns under a lock that a killed push or a
  reboot frees. One that waits longer than `delivery.push_wait_s` exits 75 and prints a
  `retry_when` for its hand-off: `ttp push --free`, which exits 0 once the turn is free. Unset,
  the wait is twice the last measured check run plus 60 s, at least 900 s and at most 2 h.
- A check is a command, which runs on every head. One that only makes sense once a file exists
  (a test file a later change adds) can opt in to being skipped where it does not apply:
  `{"run": "pytest tests/test_new.py", "if_exists": "tests/test_new.py"}`. `if_exists` is a
  repo path or glob (a directory counts by its files) looked up in the commit being checked. A
  skipped check is logged as `skipped (not applicable: ...)` by `ttp push`, the push queue and
  `ttp checks` (in `checks.log`), and never counts as passed: when every check is skipped the
  push or `ttp checks` fails. Plain string checks never skip; `after_push` takes the same form.
- A check that covers only some paths can be scoped by them:
  `{"run": "pytest -q tests", "if_changed": ["src/**", "tests/**"]}` (one glob or a list, matched
  as `delivery.push_exclude_paths` is). It is skipped, and logged as `skipped (not applicable:
  ... changes nothing under ...)`, when the diff from the push target to the head being checked
  (`git diff --no-renames <target>...<head>`, so a moved file counts under both names) touches no
  matching path, and runs normally otherwise. The push target is the fetched tip for `ttp push`,
  `ttp push --own` and the push queue, the pushed batch's previous tip for `after_push`, and the
  local copy of `delivery.push_branch` for `ttp checks`; with none to compare with, the check
  runs. A diff git cannot give (a missing ref, no merge base) fails the check. A head on which
  every check is skipped this way touches nothing any check covers, so it goes unchecked, as a
  docs-only change does without checks: a notes-only branch passes `ttp push --own` without a
  catch-all check. If any of the skips came from `if_exists` instead, nothing checked the head and
  it fails as above. A check may set both keys; it then runs only where both say it applies.
- Checks run with `TTP_PUSH_MODE` set to `target` (`ttp push` or the push queue, to
  `delivery.push_branch`), `own` (`ttp push --own`) or `checks` (`ttp checks`), and `TTP_PUSH_TIP`
  set to the tip of the branch pushed to as fetched before the checks (empty for a new branch and
  for `ttp checks`). A check that should only gate the push branch tests
  `[ "$TTP_PUSH_MODE" != target ] || ...`.
- `delivery.push_exclude_paths` (globs, off by default) keeps files off the push branch, such as
  task notes or `tmp/`: `["tmp/**", "notes/*.md"]`. `ttp push` and the push queue refuse a change
  when any commit it brings adds or modifies a matching file, and name the files; a commit that
  deletes one passes. A pattern matches a path, a directory by its files, or as an fnmatch glob
  whose `*` also crosses `/`. `ttp push --own` is not affected.
- Each check (in the push queue, `ttp push` and `ttp checks`) runs with first-failure semantics (bash `-e -o pipefail`, `sh -e` without bash), so
  `a; b` or `a | b` fails when `a` fails. Still prefer one required command per entry. A coverage
  map keyed by full commit SHA must reject unknown heads and fail when a required input is missing;
  never skip a check because of an earlier pass or failure, and add a negative control that must fail.
- tt-project runs no whitespace check of its own. A `git diff --check <base> HEAD` in
  `push_checks` also fails on committed run logs, whose captured lines keep trailing whitespace.
  Leave them out with a pathspec: `git diff --check <base> HEAD -- . ':(exclude)*.log'`. Files
  that `.gitattributes` marks `-diff` or `binary` are skipped by git already. Checks stay as
  configured; `ttp doctor` names a `git diff --check` that does not leave out `*.log`.
- A bare pytest in a check (`pytest`, `py.test`, `python3 -m pytest`, also after `env` or
  `timeout <n>`) runs as `<venv>/bin/python -m pytest` when the repository has a `.venv/` or
  `venv/` with pytest in it (in the worktree, else in its main checkout). The main checkout's venv
  is not used when it has the project installed editable from the main checkout: a worktree's tests
  would import that code and could pass falsely. The check then runs as written and its output says
  why. A check that fails at import because a package is not installed is reported by `ttp checks`
  and `ttp doctor` as an environment problem, not as the head's code (as an environment problem
  also, when tests failed too): point the check at a venv.
- `ttp checks -- <cmd>` adds a check. Several words run as that argv (`ttp checks -- pytest -q`);
  one quoted string runs through the shell (`ttp checks -- 'FOO=1 pytest -q && ruff check .'`);
  several quoted strings run as one check each (`ttp checks -- 'pytest -q' 'ruff check .'`). An
  added check that `delivery.push_checks` already runs on every head is dropped, and said, so it
  never runs twice.
- In Claude Code workers and reviewers, a Bash call that runs one of the configured pytest checks
  in full (every path it names, or a directory above them, and no `-k`, `-m`, `--lf` or node id
  it does not have) is refused with a pointer to `ttp checks`, which reuses a recorded pass.
  Focused runs pass. `TTP_ALLOW_FULL_SUITE=1` before the command (or in the run's environment)
  lets one through; each refusal is a line in the run's `refusals.jsonl`.
- Scope a mandatory check by what it covers, never by whether it would pass. Prefer `if_changed`
  on the paths the check covers, or `if_exists` on the file it runs. Where no file marks the heads a check applies to (say a test that
  is missing at an independently reviewed head of another delivery branch), the fallback is a
  shell conditional on the full SHA in the check itself:
  `h=$(git rev-parse HEAD) || exit 1; [ "$h" = <full 40-character sha> ] || pytest -q tests/test_x.py`.
  A leading assignment such as `h=$(...)` is accepted as a check's first command, like `set -e`.
  Assign first, then compare: `set -e` does not catch a failed command inside a substitution
  in a test, so `[ "$(git rev-parse --verify HEAD)" = "$expected" ]` just compares an empty
  string when git fails. Write `head=$(git rev-parse --verify HEAD) || exit 1` and then test
  `"$head"`.
  Write the condition so that an error in it runs or fails the check, never skips it. Changing
  a check's scope changes what a head was checked against: every head it affects needs a fresh
  review, and earlier approvals do not carry over. Never add blanket "skip if the test file is
  missing" guards (`[ -f tests/test_x.py ] || exit 0`, `|| true`): a missing test, a failed
  assertion and a git error (an unset identity, a bad ref) must still fail the check.
- `ttp push --detach` runs the same push in a process of its own and returns at once, for
  checks that outlast a worker's tool call. It prints a marker under `state/pushes/` and a
  `retry_when` probe, `ttp push --result <marker>`: 1 while the push runs, 0 once it finished,
  printing the pushed sha (and version) or the exit code and the log tail. A push killed by a
  crash or a reboot frees its locks; the probe (or the daemon) then records it as failed, saying
  whether it died at startup, in a reboot or later, instead of waiting. From a sandbox that runs
  each command in a PID namespace of its own (Codex on Linux), which kills a background process
  when the command ends, the push is queued and the daemon starts it; the probe exits 1 meanwhile.
- `delivery.version_bump` lets `ttp push` own the version bump, so parallel reviews never race for
  one version: `{"files": [...], "changeset_dir": ".changeset"}` (optional `package`, and `paths`,
  default the folder the files share). After each rebase, when the change touches `paths`, it
  sets every listed file one patch version above the tip's and adds a changeset when the change
  brings none, in one `<package>: X.Y.Z (<subject>)` commit; a later round replaces that commit.
- `delivery.push_queue: true` hands pushing to the daemon. A review that passes approves the
  exact head it reviewed (`"push": [{"branch", "head"}]` in its hand-off) and becomes `pushing`; it
  never runs `ttp push`. Without a model, the daemon replays the approved changes in order onto the
  latest tip of `delivery.push_branch`, adds one version bump and changeset for the batch, runs
  `delivery.push_checks` once and pushes without force. Then the reviews are `done`. Conflicts
  that need no judgment are settled in the batch and judged by those checks: version lines, lines
  both sides added at one spot, and changes of different lines that git calls a conflict only
  because they are adjacent. A change that really overlaps another goes back to its review to
  resolve the conflict; a failed check fails the
  review and wakes the coordinator. Keys, all under `delivery`: `push_batch_s` (default 900: a
  batch starts once the oldest approval waited this long; 0 = at once), `push_batch_max`
  (default 8: start at once with this many; also the most one batch takes), `push_min_gap_s`
  (default 1800: a batch starts only once the last one ended at least this long ago, measured
  from its recorded end so a restart or a failed batch never stretches the wait; a full batch or
  a priority-1 review's approval starts at once; 0 = off), `after_push`
  (commands in the same form as `push_checks`, for example a deploy) and `after_push_timeout_s`
  (default 1800). `after_push` runs after each push in a clean worktree at the pushed commit,
  with `TTP_PUSHED_SHA`, `TTP_PUSHED_VERSION`, `TTP_PUSH_TARGET`, `TTP_PUSH_TASKS`,
  `TTP_PUSH_BATCH` and `TTP_PROJECT` set. A failed `after_push` alerts the coordinator but never
  fails the reviews: their change is already on the branch. Off (the default), each review
  pushes with `ttp push --detach` as above. `ttp status` shows one line about the queue (what
  waits, the running batch, the last push and its deploy), the web app a Push queue card, and
  `ttp push --queue` lists the entries, the last 7 days' conflict counts (conflicted, resolved in
  the batch, sent back) and the last 10 batches. Only a rejected push, a failed
  `after_push` and batches that keep dying reach the top section, and only while they last.
- `delivery.backup_remote` (off by default) names a git remote that each finished code task's
  branch (`ttp/t<id>-...`) is pushed to under the same name, fast-forward only: never forced, never
  to `main`, `master`, the push branch or `delivery.base_ref` (a value naming one is refused). A
  push that is not a fast-forward is skipped with one observation. Setting it from chat needs the
  user's word. Separately, a hand-off that finds uncommitted changes to tracked paths in the
  project's main checkout records one observation naming them (once per set of paths); nothing
  there is committed or changed.
- `delivery.fast_forward_also` (branch names, off by default) keeps more branches in step with
  the push branch, such as a `main` that holds the last good state: after each successful `ttp push`
  or push queue batch, each listed branch on the push remote is moved to the pushed commit, never
  forced and only when its tip there is an ancestor of that commit. The result is read back from
  the remote and recorded in the push outcome, the probe and the landing notice as
  `ff <branch> <sha>` or `not ff <branch>: <reason>` (not an ancestor, missing, rejected). A
  `not ff` is a warning and never undoes the main push. The push branch itself and invalid names
  are refused. Like other delivery keys that widen where work lands, setting it needs the user's
  word, and a charter that forbids pushing to a listed branch wins: leave that branch out.
- A plan task starts from what is already known: prior work, the organization's docs and chats
  through the connectors you have, available skills, and public work. Skill plugins it recommends
  can be enabled for the project's workers only (`providers.claude.plugin_dirs`).
- On Claude, the part of a worker's prompt that is the same for every task (rules, charter,
  memory) goes in the system prompt, so the next worker, of any kind, reads it from the cache. The
  kind's rules and the task itself go in the user prompt.
- `providers.claude.worker_isolation: true` starts Claude workers and reviewers without your own
  MCP servers, plugins, hooks and user settings; the project's `plugin_dirs` and its hook still
  load. In one measurement it cut a worker's first turn from about 38k to 23k input tokens.
  `ttp new` turns it on; projects created earlier keep it off until you set it.
- `providers.claude.mcp_servers: ["name", ...]` lists the MCP servers isolated workers still get.
  Each run copies just those entries from your Claude config (local, then `.mcp.json`, then user
  scope) into its own owner-only file in the temp directory, and deletes it when the run ends.
  Listing a server approves it for workers. A name your config does not define is skipped: the
  run still starts, and `ttp doctor` and a low alert name it. Servers that come from a Claude plugin
  are not in those files, so they cannot be listed; add the server with `claude mcp add` to list it.
- `providers.codex.worker_isolation: true` starts Codex workers and reviewers with
  `--ignore-user-config`, so your `config.toml` (its MCP servers, plugins and hooks) stays out;
  sign-in still works. Codex coordinator turns always skip it, and your rules files too, where the
  build has those flags. A config that sets your own model provider, API or ChatGPT endpoint,
  login method or credentials store is kept, since runs need it.
  Codex has no per-run MCP list, so isolated Codex workers get no MCP servers of yours.

## Where things live

| Path | Holds |
|---|---|
| `<project root>/tt-project/` | everything for the project; ignores itself, so nothing gets committed |
| `…/harness/` | the project's own harness (git): charter, memory, config, prompts, runtime |
| `…/state/` | database, run directories, logs |
| `…/worktrees/` | one git worktree per code task |
| `~/.tt-project/` | per-user registry of projects, secrets (mode 0600), the `ttp` install, account-level settings (`settings.json`), the global spend cache (`global-spend.json`) |

Each project starts from this plugin's template and then improves its own harness from
experience. When a newer tt-project is installed (`ttp setup` from the updated plugin; the skill does
it on first use after an update; to deploy a branch tip, run `plugins/tt-project/bin/ttp setup` from a
checkout at that tip, since the installed `ttp setup` reinstalls its own version and warns about it), each project's daemon merges it into its own harness within an hour
and restarts, keeping running work. `ttp config <name> upgrade.auto false` turns that off;
`ttp upgrade <name>` then merges by hand. `ttp upgrade` exits 75 when it changed nothing and the
project finishes the upgrade itself (a merge conflict handed to its harness task, or retried by its
daemon under `upgrade.auto`, which retries only a newer version), so a deploy script can count it as
deferred; exit 1 is a real failure, such as a conflict nothing retries.
Most conflicts settle without a model: changes to different lines, additions at one spot, and a
local edit that upstream now makes too (comments and spacing aside: upstream's version is taken,
the project's stays in the merge's first parent; a line the project removed and upstream reworded
is a real overlap). A project's own rules in a template prompt belong
between `<!-- ttp:local -->` and `<!-- /ttp:local -->` lines (the prompt leaves those lines out):
an upgrade takes upstream's text and puts each block back where it stood. When a project only added
lines to a template prompt, the upgrade fences them that way itself. Only a real overlap, such as
the project and upstream rewording the same line, goes to a harness task.
The same conflict deferred past 48 h, or past two harness tasks that ended without landing it,
reaches the coordinator once as a normal observation, not once per deploy.

## Notifications

Alerts go to every attached chat, to the web app (one-click browser notifications), and, if you
install it, to a desktop notifier on your workstation that covers all your projects
(`ttp notifier install`). Only decisions, reviews, merges, funds and outages notify by default.
Workers can read Slack links you paste, using your Slack connector if you have one.

## Security

- The web app listens on localhost with a per-project token. From another machine,
  `ttp web <name> --tunnel --keep` opens and keeps an SSH local forward to it.
- Workers run with your permissions, in their own worktree; restrictions in the charter are
  passed to every worker and enforced by the provider where it can (for example, no web tools).
- Text from logs, issues and chats is treated as data, not instructions.
- Secrets are entered in a terminal (`ttp secret …`), never in chat, and never copied into a project.

## Maintainers

Design notes, invariants and how to add a provider: [docs/design.md](docs/design.md).

## Limits

- Codex and Cursor report tokens but no cost; their spend is estimated from a price table. Set
  your own rates in project.json as `"pricing": {"codex": {"<model>": [input, cached input,
  output]}}` in $ per million tokens (`"default"` covers other models; same for `cursor`).
- A Claude run cut off before its final report is estimated from its streamed tokens. Codex
  reports usage per completed turn, so its mid-run budget check sees completed turns only. Cursor
  builds that offer `--output-format stream-json` stream their progress: the stall guard sees it,
  and until usage arrives the budget check uses a floor from the text written so far. Older
  Cursor builds print nothing until the end, so set `stall_s` above their longest run. A run cut
  off before reporting usage is booked at the elapsed share of its budget (flagged estimated).
  A run that wrote no output and reported no tokens is booked at $0 (except on an older Cursor
  build, which prints nothing until it ends).
- Codex workers run in Codex's workspace-write sandbox, with the project's state folder, the
  repository's git folder and the shared lock folder (`~/.tt-project/locks`, for `ttp lock` on
  resources shared across projects) added as writable roots. Coordinator turns use its read-only
  sandbox.
- Codex and Cursor coordinator turns run from an empty scratch directory, so the project's
  AGENTS.md and rules stay out of them. Codex's shell and web search tools are off, and Cursor
  runs in ask mode, when the installed CLI offers those switches.
- Codex workers get the harness hook (coordinator updates handed over once, PR draft guard,
  full-suite and `ttp say` refusals) as per-run config, never by editing your Codex home. Codex
  runs such a hook only when trusted, so the run trusts it for that call alone, and only when the
  hook is the run's sole hook source: if your Codex home, the repository's `.codex/` folder or an
  enabled plugin may add hooks, the worker runs without it and reads `steer.md` between steps (an
  isolated worker skips your config, so only the home `hooks.json` and the repository count then).
  A build without hooks or the trust flag keeps that older behaviour. Tested live on codex-cli 0.160.
- Not on Codex: worker plugins (`plugin_dirs`). Codex loads plugins only from those installed in
  its home (`codex plugin add` copies them there), so a run cannot add one for itself without
  changing your Codex setup; plugins you install yourself load as usual.
- Not yet on Cursor: worker plugins, worker isolation, coordinator updates reaching a running
  worker, and isolation of coordinator turns from your own CLI config and MCP servers. Cursor
  enforces no `no_internet` restriction and has no plan-window meter; without ask mode it has no
  read-only mode either.
- Cursor has no reasoning-effort flag; tiers map to model names.
- Context compaction per tier (`budget.compact_window_tokens`) works on Claude Code and Codex.
- Long command output stays out of a worker's context: `ttp clip -- <cmd>` and `ttp checks` keep
  it in a file and show a test run's failures (else head and tail). On Claude Code, Bash output
  past `budget.bash_output_max_chars` also goes to a file. A worker whose run re-reads more than
  `budget.split_reread_tokens` of context is told once to hand the rest on as a follow-up.
- `ttp killscan <script>` flags kills by name or pattern (`pkill`, `killall`, any `pgrep` or
  `pidof`, `ps | grep` feeding a kill) before a worker runs a script; `--shim <dir>` writes
  stand-ins for `pkill`, `killall`, `pgrep` and `pidof` that only log the call (`ps | grep` and calls
  by absolute path must be edited out). `pkill -P <pid>` is not flagged. A pattern kill in a script
  can match the worker's own tool shell.
- Resuming a lost run's session works on Claude Code and Codex; Cursor starts fresh.
- A laptop pauses while it sleeps. Use an always-on machine for round-the-clock work.
