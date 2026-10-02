# Operating a project

## Commands

| Command | Does |
|---|---|
| `ttp list` | projects known on this machine |
| `ttp find <name>` | locate by registry, then by chat-log locators |
| `ttp adopt <name> --host H --dir D` | record where a project lives |
| `ttp status [<name>] [--json]` | daemon, spend vs caps, coordinator health, why idle, running/blocked/waiting tasks, open questions, a newer tt-project release; without a name, the project of the current folder |
| `ttp task <name> list` / `add "<title>" --spec …` | inspect or queue work by hand |
| `ttp memory <name> "<fact>" [--kind K]` / `--forget <entry>` | add a durable fact / retire a stale one to memory/archive/. Prompts get restrictions, preferences and resources whole, then the newest decisions and facts that fit |
| `ttp config <name> <key> [value]` | read or set settings (dotted keys) |
| `ttp pause <name>` / `resume` | stop starting model runs / start again |
| `ttp machines add <alias> --tags device,... [--note ...] [--min-free-gb N] [--hostname H]` / `ttp machines list` / `ttp machines remove <alias>` | the user's machines, shared by all their projects; each charter says which ones a project may use. `--min-free-gb` sets the disk guard on that machine's filesystem for every project there (0 = off, "" = the projects' own); `--hostname` when the alias is not its short host name |
| `ttp pause <name> --resource <r> [--reason ...]` / `ttp resume <name> --resource <r>` | hold every task that uses resource `<r>` and make `ttp lock <r>` refuse it; running workers on it are told / lift it |
| `ttp upgrade <name>` | merge the installed tt-project release into the harness and restart; the daemon does this by itself unless `upgrade.auto` is false |
| `ttp restart <name>` | restart the daemon and confirm it runs; a runtime it cannot start with is rolled back |
| `ttp stop <name> [--kill]` | remove the service; keeps all data. Running workers finish unless `--kill` (their tasks resume on start) |
| `ttp task <name> cancel <id>` | cancel a task and end its running worker |
| `ttp doctor <name>` | providers, accounts, Jev, notifications, web |
| `ttp alerts <name> --after N` | alerts since a message id |
| `ttp web <name> --tunnel --keep` / `--unkeep` | keep the web app's tunnel up as a user service / remove it (remote projects; ask first) |

## What needs the user

- The top of `ttp status` and of the web app shows only open questions and problems active now.
  Everything else (FYI notes, decisions, reboots, cleared alerts) is in the feed below, newest first.
- Alerts clear themselves and keep their history: logged out → the next successful run; budget red →
  the gate leaves red; coordinator failures → a successful turn; disk low → space is back. Chats
  hear once that it cleared. A host reboot is information only.
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
  one high alert per episode, cleared once free space is 1.2× the threshold. Status, the web app and
  the coordinator's digest show free space. A machine whose entry in the machines list has
  `min_free_gb` uses that instead of `disk.min_free_gb` (for a shared disk that other services keep
  near full by design). The alert, and the digest while low, say how much of the used space is this
  project's own data and name the biggest top-level directories (`du -x -d 1`, stopped after 30 s),
  so a full shared disk is not taken for project growth.
- When a task ends (done, failed, cancelled), its worktree loses its git-ignored build and cache
  directories (`disk.cache_dirs`) and is removed once clean with HEAD on a branch. Branches are never
  deleted, so `continues` still starts from the old commits. A dirty worktree is kept and listed, and
  so is one with submodules set up (their commits may exist only there), and one holding git-ignored
  files that any task's hand-off (`result.json` `artifacts`, globs too) lists; cache clearing skips those. A worktree is
  left untouched for at least an hour after its task ends, until the coordinator has seen the result,
  and while an unfinished task still needs it (it depends on or continues the task, or its spec names
  the task's branch, id or `worktrees/tN` path). `disk.worktree_retention_days` waits longer; 0 never tidies.
  `ttp prune <name>` sweeps now.
- Work only on this machine: when a code task hands off done, and hourly for code tasks done in the
  last 14 days, the daemon fetches (30 s timeout) and checks whether the task branch's work is on a
  remote: a remote-tracking branch contains its head, or its changes are already on the delivery
  branch or the remote's default branch (rebased, cherry-picked, amended or batched by a reviewer).
  Tasks an unfinished task still needs (a queued review or fix), tasks a done review names, and work
  done before the check first ran are left alone. Otherwise one coordinator event says so, and
  status and the web app count it until the work reaches a remote, the task is cancelled or it is
  14 days old. Nothing is pushed automatically; a repository without a remote is skipped.
- Coordinator failing repeatedly → an alert says so; messages are kept, not lost.
- A worker that produces nothing for too long is stopped and retried (stall guard).
