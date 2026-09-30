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
  When the coordinator rescopes a running task, the change reaches the worker mid-run.
- **Memory and charter**: plain files in the project's harness, one fact per file.
- **Watchers**: pull requests (CI, reviews, mergeability) and logs, reporting only changes.
  With Jev enabled, new observations are screened by a cheap decision model first.

## Budget

- Subscription plans: a plan's capacity is lost at each reset, so the project uses it. It reads
  the account's live window usage, measures how fast the account is burning it, and paces itself
  to land each window near 90% by its reset: more parallel workers while there is room, fewer when
  the pace would overshoot. It never goes past 90%; the rest stays yours.
- Usage-billed accounts: $100 per 24 hours and $200 per 7 days per project by default. A new run
  starts only if its budget fits in what is left of both caps. A plan account whose runs stop
  reporting plan windows falls under these caps too.
- Work backs off in steps as spend rises, pauses at the cap, and tells you how to raise it.
- A runaway guard pauses a project whose hourly spend jumps far above its own norm.
- The web app shows spend per day, per task and per recurring job, and plan-window peaks for the
  last two weeks.

## Parallel work

- Up to 6 workers per project run side by side (`budget.max_parallel_workers`); on a plan, the
  pacing sets the actual number. When a plan is under pace, slots sit idle and nothing is queued,
  the coordinator is asked for more work, less often each time it finds none.
- A shared device or machine is taken per command, through its own queue (for example a device
  broker) or `ttp lock <resource> -- <command>`, so the rest of each task runs in parallel. A task
  marked exclusive holds the resource's lock for its whole run; while it waits for a slot, new
  `ttp lock` commands wait behind it, so it is not starved.
- Each task edits only its own worktree.
- Where reviewed changes go straight to a shared branch, `ttp push` publishes them guarded: it
  refuses uncommitted changes, rebases onto the latest tip, runs `delivery.push_checks` on the
  exact commit it pushes, starts over if the branch moved meanwhile, and never forces. The target
  is `delivery.push_branch`, which must be set explicitly; it refuses without one, without
  checks, when `delivery.push_allowed` is false, and for `HEAD`, `main`, `master` or the
  remote's default branch.
- A plan task starts from what is already known: prior work, the organization's docs and chats
  through the connectors you have, available skills, and public work. Skill plugins it recommends
  can be enabled for the project's workers only (`providers.claude.plugin_dirs`).
- On Claude, the part of a worker's prompt that is the same for every task of its kind (rules,
  charter, memory) goes in the system prompt, so the next worker reads it from the cache.
- `providers.claude.worker_isolation: true` (off by default) starts Claude workers without your
  own MCP servers, plugins, hooks and user settings; the project's `plugin_dirs` and its hook still
  load. In one measurement it cut a worker's first turn from about 38k to 23k input tokens.

## Where things live

| Path | Holds |
|---|---|
| `<project root>/tt-project/` | everything for the project; ignores itself, so nothing gets committed |
| `…/harness/` | the project's own harness (git): charter, memory, config, prompts, runtime |
| `…/state/` | database, run directories, logs |
| `…/worktrees/` | one git worktree per code task |
| `~/.tt-project/` | per-user registry of projects, secrets (mode 0600), the `ttp` install |

Each project starts from this plugin's template and then improves its own harness from
experience. `ttp upgrade <name>` merges later template versions into it.

## Notifications

Alerts go to every attached chat, to the web app (one-click browser notifications), and, if you
install it, to a desktop notifier on your workstation that covers all your projects
(`ttp notifier install`). Only decisions, reviews, merges, funds and outages notify by default.
Workers can read Slack links you paste, using your Slack connector if you have one.

## Security

- The web app listens on localhost with a per-project token. Use an SSH forward from elsewhere.
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
- A laptop pauses while it sleeps. Use an always-on machine for round-the-clock work.
