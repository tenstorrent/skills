# Operating a project

## Commands

| Command | Does |
|---|---|
| `ttp list` | projects known on this machine |
| `ttp overview [--json]` / `ttp list --status` | one line per project on this machine: daemon running, stopped or stale; harness version, flagged when behind the installed release; open asks; tasks waiting on the user; running workers; spend today vs its cap; then the global daily cap. Other projects are only read; remote ones show "remote, not checked". The web app's Projects tab shows the same |
| `ttp find <name>` | locate by registry, then by chat-log locators |
| `ttp adopt <name> --host H --dir D` | record where a project lives |
| `ttp status [<name>] [--json]` | daemon, spend vs caps, coordinator health, why idle, running/blocked/waiting tasks, open questions, a newer tt-project release; without a name, the project of the current folder |
| `ttp task <name> list` / `add "<title>" --spec …` | inspect or queue work by hand |
| `ttp memory <name> "<fact>" [--kind K] [--standing]` / `--forget <entry> [--why W]` | add a durable fact / retire a stale one to memory/archive/. Prompts get restrictions, preferences, resources and standing entries whole, then the newest decisions and facts that fit. `--standing`: a duty that recurs; retiring it needs `--why` (its end condition or the user's words ending it) |
| `ttp config <name> <key> [value]` | read or set settings (dotted keys) |
| `ttp pause <name>` / `resume` | stop starting model runs / start again |
| `ttp machines add` (or `set`) `<alias> --tags device,... [--note ... [--until 3d]] [--min-free-gb N] [--hostname H] [--shared [names]]` / `ttp machines list` / `ttp machines remove <alias>` | the user's machines, shared by all their projects; each charter says which ones a project may use. A note's `--until` marks it stale in the coordinator's digest once passed. `--min-free-gb` sets the disk guard on that machine's filesystem for every project there (0 = off, "" = the projects' own); `--hostname` when the alias is not its short host name. `--shared` makes its resources (default: the alias) one set of lock slots and one pause across all projects (also: config `shared_resources`); where the projects' `resources` counts differ, all use the smallest. A paused shared resource cannot be unshared or removed until resumed; one dropped from `shared_resources` stays paused in that project |
| `ttp pause <name> --resource <r> [--reason ...] --until <2d or ISO time> \| --end-when <probe> [--report-from <project>]` / `ttp resume <name> --resource <r>` | hold every task that uses resource `<r>` and make `ttp lock <r>` refuse it; running workers on it are told / lift it. An end (at most 7 days, or a read-only probe) is required; when it passes the coordinator lifts or extends the pause, except one the user set, which stays until the user lifts it |
| `ttp lock <r> -- <cmd>` / `ttp lock --probe <r>` / `ttp detach <job> -- <cmd>` | run one command holding resource `<r>` (waiters in arrival order; 75 when busy or paused) / exit 0 if `<r>` is free now, 75 if not / start a job that outlives the worker's run; the task then waits on `ttp detach --check`, and an `exclusive:` task's resources stay held until the job ends (`--remote <ssh alias>` starts it on another host; its probe is `ttp detach --check --host <alias> <dir>/<name>`) |
| `ttp ci --branch <b>` / `--run <id>` | `retry_when` for GitHub CI: exit 0 once the commit's runs completed or a job hung (past 3x its recent median, floor 10 min, 90 min with no history), 1 while they run, 75 if gh cannot answer; prints `done:`/`hung:`/`running:` per run |
| `ttp upgrade <name>` | merge the installed tt-project release into the harness and restart; the daemon does this by itself unless `upgrade.auto` is false |
| `ttp restart <name>` | restart the daemon and confirm it runs; a runtime it cannot start with is rolled back; from a sandbox it asks the running daemon to restart (exit 75 while deferred, 1 on failure) |
| `ttp stop <name> [--kill]` | remove the service; keeps all data. Running workers finish unless `--kill` (their tasks resume on start) |
| `ttp task <name> cancel <id>` | cancel a task and end its running worker |
| `ttp task <name> set-when <id> "<cmd>"` | re-point the probe of a task that has not started: a waiting task's `retry_when`, else its `start_when`; `""` clears it. Refuses running and finished tasks |
| `ttp landed <sha>` / `ttp landed --task <id>` | probe: exit 0 once the commit, or the task's landing, is on the push branch, also after a rebase gave it a new sha (same patch-id, or same author, date and subject); 1 not yet. `start_when` `landed:#<id>` runs it |
| `ttp doctor <name>` | providers, accounts, Jev, notifications, web |
| `ttp alerts <name> --after N` | alerts since a message id |
| `ttp stats [<name>] [--days N] [--json]` | context re-read (cache-read) tokens per run and per $, by role, task kind, tier and effort, and the runs that re-read most |
| `ttp audit [<name>] [--hours N] [--json]` | the daily review's self-efficiency audit: every ask with its blocking reason and answer, failed, lost, retried and continued runs, idle wakes, $ per done task by kind and tier, review-loop spend per change, stuck work and overrides held past their end |
| `ttp web <name> --tunnel --keep` / `--unkeep` | keep the web app's tunnel up as a user service / remove it (remote projects; a local view forward needs no ask) |

## What needs the user

- The top of `ttp status` and of the web app shows only open questions and problems active now.
  Everything else (FYI notes, decisions, reboots, cleared alerts) is in the feed below, newest first.
- Alerts clear themselves and keep their history: logged out → a login check passes (until then no
  run starts on that provider; the daemon asks its CLI's status, no model call); budget red →
  the gate leaves red; coordinator failures → a successful turn; disk low → space is back. Chats
  hear once that it cleared. A host reboot is information only.
- A logout is one alert per machine and provider, however many projects wait on it, reminded after
  1 h, 4 h and 12 h, then daily, with the waiting projects, queued tasks and high events.
- While the coordinator cannot run (logged out, paused, budget red, repeated failures) for 30 min,
  the daemon sends new high conditions that persisted 2 h to the chat itself: one line each, at
  most one message an hour.
- The budget is a few plain lines: per plan window the percent used, time to reset and history
  (daily peaks for the 5-hour window, the last two weekly finals), and one line for dollar caps.
  Pacing, gate reasons and top spenders are in the web app's Budget tab.

## Budget

- Plan windows (subscription): the project stops at 90% of any window.
- Usage-billed: default caps $100 per 24 h and $200 per 7 days, per project (all providers together).
- A run cut off before it reports its cost counts at an estimate, labelled as such.
- Gates tighten as spend rises: `green` → `yellow` → `orange` → `red` (paused).
- A spend spike far above the project's norm pauses it ("runaway guard").
- Raising a cap: tell the coordinator, or `ttp config <name> budget.daily_usd <n>`.
  Warn the user to raise caps carefully.

## Health

- Daemon not running → its service restarts it. "Stuck" (up but no completed tick for 10 min) → its
  watchdog restarts it (systemd `WatchdogSec`; launchd and cron run `ttp.watchdog` every 5 min). Only a
  service installed before the watchdog needs `ttp restart <name>` (which adds it); still down →
  `ttp logs <name>`.
- Only one daemon runs per project (a lock file), whatever starts it.
- Disk guard: free space under the project folder below the smaller of `disk.min_free_pct` (5) of
  the disk and `disk.min_free_gb` (150) → only question and plan tasks start; running work goes on;
  one high alert per episode, cleared once free space is back at `disk.resume_free_gb` (unset, or
  at least the disk's size: 1.2× the threshold; below the threshold: the threshold; a value below
  `disk.min_free_gb` makes `ttp doctor` warn), so a disk hovering at the line does not flap; the episode survives a daemon restart. Status, the web app and
  the coordinator's digest show free space. A machine whose entry in the machines list has
  `min_free_gb` uses that instead of `disk.min_free_gb` (for a shared disk that other services keep
  near full by design) and resumes at 1.2× it. The alert, and the digest while low, say how much of
  the used space is this project's own data and name the biggest top-level directories (`du -x -d 1`,
  stopped after 30 s and killed; never from / or a home folder, nor over a network or FUSE mount:
  those show as unknown), so a full shared disk is not taken for project growth.
- When a task ends (done, failed, cancelled), its worktree loses its git-ignored build and cache
  directories (`disk.cache_dirs`) and is removed once clean with HEAD on a branch. Branches are never
  deleted, so `continues` still starts from the old commits. One whose only dirty entries are untracked
  files (HEAD on a branch) has them moved to its last run's `worktree-leftovers/` (relative paths kept)
  and is removed, if they total at most `disk.worktree_leftovers_max_mb` (50; 0 never). One with
  modified tracked files is kept and raised to the coordinator once per content (event
  `worktree_uncommitted`); `ttp status` counts them and the web app lists them in the feed. Any other
  dirty worktree is kept and listed, and so is one with submodules set up (their commits may exist only there), and one holding git-ignored
  files that any task's hand-off (`result.json` `artifacts`, globs too) lists; cache clearing skips those. A worktree is
  left untouched for at least an hour after its task ends, until the coordinator has seen the result,
  and while an unfinished task still needs it (it depends on or continues the task, or its spec names
  the task's branch, id or `worktrees/tN` path). `disk.worktree_retention_days` waits longer; 0 never tidies.
  `ttp prune <name>` sweeps now (`--dry-run` lists what it would do). Each keep reason is logged once.
- Workers and reviewers run with the project's Python venv active (`VIRTUAL_ENV`, `PATH`; their
  prompt names it), so a fresh worktree does not rebuild one. `worktree.venv`: `auto` (default) finds
  `.venv` or `venv` in the project root, a path names another, `""` turns it off. A worktree with a
  venv of its own keeps it; with no venv, runs start as before.
- Every worktree of the project's repository (a code or fix task's, one a reviewer works in or made
  itself, where `ttp checks` or `ttp push` runs, and the push queue's checkouts) gets a symlink to
  each `worktree.link_paths` entry (default `[".venv"]`,
  relative to the project root) that exists and is git-ignored in the project checkout and is
  missing in the worktree, so a check such as `.venv/bin/python -m pytest` works there. Tracked
  paths and existing files are never touched; the link is git-ignored (info/exclude if needed), and
  removing the worktree deletes only the link. `[]` turns it off. Check commands can also use
  `"$VIRTUAL_ENV"/bin/python`: workers and reviewers run with the project venv active.
- Work only on this machine: when a code task hands off done, and hourly for code tasks done in the
  last 14 days, the daemon fetches (30 s timeout) and checks whether the task branch's work is on a
  remote: a remote-tracking branch contains its head, or its changes are already on the delivery
  branch or the remote's default branch (rebased, cherry-picked, amended or batched by a reviewer).
  Tasks an unfinished task still needs (a queued review or fix), tasks a done review names, and work
  done before the check first ran are left alone. Otherwise one coordinator event says so, and
  status and the web app count it until the work reaches a remote, the task is cancelled or it is
  14 days old. Nothing is pushed automatically unless `delivery.backup_remote` names a git remote:
  then each done code task's branch is pushed there, fast-forward only (never forced, never to main,
  the push branch or the base_ref). A repository without a remote is skipped. A hand-off also
  notes, once per set of paths, uncommitted changes to tracked files in the main checkout.
- Project root left changed by a run → one low alert (`root-checkout:<task>`, plus a coordinator
  event) when a worker or reviewer run ends with the root's checkout on another branch than as it
  started, or with tracked paths newly uncommitted. One alert per change, not per run that saw it:
  it blames the runs that worked in the project root (else the run that ended) and lists the rest
  as also running; it changes nothing and clears once the checkout is back and those paths are clean.
  Code tasks always get their own worktree; `worktree.kinds` (default `[]`) gives other kinds one
  too (say `["work"]`; never review or harness). Off by default because a fresh worktree lacks the
  root's untracked build trees, outputs and submodule checkouts that work tasks often use. Such a
  task's branch gets the local-only, backup and integrity checks of a code task, but no review.
- Coordinator failing repeatedly → an alert says so; messages are kept, not lost.
- A worker that produces nothing for too long is stopped and retried (stall guard).
