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

- Subscription plans: a plan's capacity is lost at each reset, so the project uses it. It reads
  the account's live window usage, measures how fast the account is burning it, and paces itself
  to land each window near 90% by its reset: more parallel workers while there is room, fewer when
  the pace would overshoot. It never goes past 90%; the rest stays yours.
- Over pace, deep tasks run at the standard tier. When even one worker is too many, new starts are
  spaced out: if the pace allows a fraction `f` of a worker, the next start waits the last run's
  length x (1/f - 1) after it ended, at most `budget.max_pace_hold_s` (default 2 hours); a wait,
  once set, only ever moves earlier. Burn is measured over up to 12 hours, and a window stays over
  pace until its burn falls below 85% of the pace, so whole-percent readings do not flip it. Your own
  tasks and reviews of finished work start anyway, running work is never stopped, and the wait
  wakes no coordinator. `ttp status` and the web app show `paced: next start ~HH:MM (...)`.
- Usage-billed accounts: $100 per 24 hours and $200 per 7 days per project by default. A new run
  starts only if its budget fits in what is left of both caps. A plan account whose successful
  runs stop reporting plan windows falls under these caps too (failed or silent runs do not count).
- Work backs off in steps as spend rises, pauses at the cap, and tells you how to raise it.
- A runaway guard pauses a project whose hourly spend jumps far above its own norm.
- A review runs light when the diff it checks touches no `review.risky_paths` glob and is doc-only
  or small (`review.light_max_lines` non-doc lines, default 60), standard otherwise; only the
  coordinator picks deep. A re-review after a failed review is measured from the head that review
  recorded (`metrics.reviewed_head`), so a small fix on a large stack runs light.
- The web app shows spend per day, per task and per recurring job, and plan-window peaks for the
  last two weeks.
- The web app's header and `ttp status` show the budget in one line, for example
  `5h 4% - resets in 3.9 h, 7d 21% - resets in 6.0 d, 24h $0.17 virtual, 5h avg 31%, 7d avg 72%`.
  The windows and averages are the account's (an average is the mean of each completed window's
  peak: 5-hour windows over 7 days, weekly ones over 3 weeks). The dollars are this project's last
  24 h: `virtual` (list-price equivalent) on a plan, `actual` when billed by use.

## Parallel work

- Up to 6 workers per project run side by side (`budget.max_parallel_workers`); on a plan, the
  pacing sets the actual number. When a plan is under pace, slots sit idle and nothing is queued,
  the coordinator is asked for more work, less often each time it finds none.
- A shared device or machine is taken per command, through its own queue (for example a device
  broker) or `ttp lock <resource> -- <command>`, so the rest of each task runs in parallel. A task
  marked exclusive holds the resource's lock for its whole run; while it waits for a slot, new
  `ttp lock` commands wait behind it, so it is not starved. Locks and pauses are per project; a
  resource that several projects on one machine use is declared shared (`shared_resources` in the
  config, or `ttp machines add <alias> --shared [names]`), and then all of them take turns on its
  slots, a pause of it holds in each, and `ttp status` shows which project holds or paused it.
- Each task edits only its own worktree. A code task branches from the first of:
  `delivery.base_ref`; `delivery.push_branch`, then a branch the charter names as
  `branch <name>`, each only if it exists here or on origin; the remote's default branch
  (`origin/HEAD`); the checked-out branch.
- Where reviewed changes go straight to a shared branch, `ttp push` publishes them guarded: it
  refuses uncommitted changes, rebases onto the latest tip, runs `delivery.push_checks` on the
  exact commit it pushes, starts over if the branch moved meanwhile, and never forces. The target
  is `delivery.push_branch`, which must be set explicitly; it refuses without one, without
  checks unless the change touches only docs (`*.md`, `*.rst`, `docs/`, ...), when
  `delivery.push_allowed` is false, and for `HEAD`, `main`, `master` or the
  remote's default branch. Pushes to one branch take turns under a lock that a killed push or a
  reboot frees. One that waits longer than `delivery.push_wait_s` (default 300 s) exits 75 and
  prints a `retry_when` for its hand-off: `ttp push --free`, which exits 0 once the turn is free.
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
it on first use after an update), each project's daemon merges it into its own harness within an hour
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
- Codex workers run in Codex's workspace-write sandbox, with the project's state folder and the
  repository's git folder added as writable roots. Coordinator turns use its read-only sandbox.
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
