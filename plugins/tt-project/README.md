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
- **Memory and charter**: plain files in the project's harness, one fact per file.
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
- Work backs off in steps as spend rises, pauses at the cap, and tells you how to raise it.
- A runaway guard pauses a project whose hourly spend jumps far above its own norm.
- A review runs light when the diff it checks touches no `review.risky_paths` glob and is doc-only
  or small (`review.light_max_lines` non-doc lines, default 60), standard otherwise; only the
  coordinator picks deep. A re-review after a failed review is measured from the head that review
  recorded (`metrics.reviewed_head`), so a small fix on a large stack runs light.
- When delivery has a review step (`delivery.review_before_pr` or a `delivery.push_branch`), the
  daemon queues the review of each finished code task whose hand-off has no follow-ups, notes or
  findings itself (`review.auto`, on by default), and that hand-off starts no coordinator turn.
  Any other hand-off leaves the review to the coordinator's turn. `review.auto_notes` is added
  to each such review's spec (for example, what to do after a push). A review the coordinator adds
  for the same work replaces the daemon's while it has not started. A review that fails with
  follow-ups gets one fix task on the reviewed branch and a re-review from the daemon, and the
  tasks waiting on the failed review wait on the re-review instead of being blocked (at most two
  rounds per stack; a failure without fix follow-ups still blocks them, and `upstream:` notes and
  deferred follow-ups stay with the coordinator).
- Each tier maps to a model and effort per provider (`providers.<name>.tiers`). The coordinator
  runs at `coordinator.tier` (light) unless `coordinator.model` or `coordinator.effort` is set:
  those pin the coordinator alone, so moving the light tier to a cheaper model does not move it.
  Empty (the default) follows the tier. The model names one of `core_provider`'s models.
  A turn about stuck work (a blocked or failed task, resource trouble, an ask timing out, an idle
  wake that finds blocked tasks or open asks) runs at least at `coordinator.unblock_effort` (high;
  empty turns this off), so it looks harder for a non-disruptive way forward.
- The web app shows spend per day, per task and per recurring job, and plan-window peaks for the
  last two weeks.
- The web app's header and `ttp status` show the budget in one line, for example
  `5h 4% - resets in 3.9 h, 7d 21% - resets in 6.0 d, 24h $0.17 virtual, 5h avg 31%, 7d avg 72%`.
  The windows and averages are the account's (an average is the mean of each completed window's
  peak: 5-hour windows over 7 days, weekly ones over 3 weeks). The dollars are this project's last
  24 h: `virtual` (list-price equivalent) on a plan, `actual` when billed by use.

## Parallel work

- Up to 6 workers per project run side by side (`budget.max_parallel_workers`); on a plan, all of
  them until the last stretch before the line. When a plan is below its line, slots sit idle and
  nothing is queued, the coordinator is asked for more work, less often each time it finds none.
- A shared device or machine is taken per command, through its own queue (for example a device
  broker) or `ttp lock <resource> -- <command>`, so the rest of each task runs in parallel. A task
  marked exclusive holds the resource's lock for its whole run; while it waits for a slot, new
  `ttp lock` commands wait behind it, so it is not starved. Locks and pauses are per project; a
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
- Where reviewed changes go straight to a shared branch, `ttp push` publishes them guarded: it
  refuses uncommitted changes, rebases onto the latest tip, runs `delivery.push_checks` on the
  exact commit it pushes, starts over if the branch moved meanwhile, and never forces. It exits 4
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
  `delivery.push_checks` once and pushes without force. Then the reviews are `done`. A change that
  no longer rebases goes back to its review to resolve the conflict; a failed check fails the
  review and wakes the coordinator. Keys, all under `delivery`: `push_batch_s` (default 900: a
  batch starts once the oldest approval waited this long; 0 = at once), `push_batch_max`
  (default 8: start at once with this many; also the most one batch takes), `after_push`
  (commands in the same form as `push_checks`, for example a deploy) and `after_push_timeout_s`
  (default 1800). `after_push` runs after each push in a clean worktree at the pushed commit,
  with `TTP_PUSHED_SHA`, `TTP_PUSHED_VERSION`, `TTP_PUSH_TARGET`, `TTP_PUSH_TASKS`,
  `TTP_PUSH_BATCH` and `TTP_PROJECT` set. A failed `after_push` alerts the coordinator but never
  fails the reviews: their change is already on the branch. Off (the default), each review
  pushes with `ttp push --detach` as above.
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

## Where things live

| Path | Holds |
|---|---|
| `<project root>/tt-project/` | everything for the project; ignores itself, so nothing gets committed |
| `…/harness/` | the project's own harness (git): charter, memory, config, prompts, runtime |
| `…/state/` | database, run directories, logs |
| `…/worktrees/` | one git worktree per code task |
| `~/.tt-project/` | per-user registry of projects, secrets (mode 0600), the `ttp` install |

Each project starts from this plugin's template and then improves its own harness from
experience. When a newer tt-project is installed (`ttp setup` from the updated plugin; the skill does
it on first use after an update; to deploy a branch tip, run `plugins/tt-project/bin/ttp setup` from a
checkout at that tip, since the installed `ttp setup` reinstalls its own version and warns about it), each project's daemon merges it into its own harness within an hour
and restarts, keeping running work. `ttp config <name> upgrade.auto false` turns that off;
`ttp upgrade <name>` then merges by hand.

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
- Not yet on Codex or Cursor: worker plugins (`plugin_dirs`), worker isolation, coordinator
  updates reaching a running worker (Claude hooks only), and isolation of coordinator turns from
  your own CLI config and MCP servers. Cursor enforces no `no_internet` restriction and has no
  plan-window meter; without ask mode it has no read-only mode either.
- Cursor has no reasoning-effort flag; tiers map to model names.
- Context compaction per tier (`budget.compact_window_tokens`) works on Claude Code and Codex.
- Resuming a lost run's session works on Claude Code and Codex; Cursor starts fresh.
- A laptop pauses while it sleeps. Use an always-on machine for round-the-clock work.
