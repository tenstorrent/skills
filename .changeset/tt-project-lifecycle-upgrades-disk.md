---
"tt-project": patch
---

`tt-project`: safe stops, restarts and upgrades, and disk hygiene.

- One daemon per project, enforced by a lock the kernel releases when the daemon dies; a stale
  pid file can no longer block a start.
- The daemon records a heartbeat after every tick. `ttp status` and the web app say when it is
  not running or stuck, and the web app says when it cannot reach the daemon.
- `ttp stop` and `ttp restart` leave running workers alone; the next daemon adopts them.
  `ttp stop --kill` ends them, and their tasks resume on the next start without spending an attempt.
- Cancelling a task from the CLI, web app or coordinator ends its running worker within seconds.
- `ttp upgrade` merges in a scratch worktree and applies the result only when it is clean and the
  runtime compiles and imports; otherwise the harness is unchanged and a harness task is queued.
- `ttp restart` waits for the daemon to tick. If the new daemon never starts, exits, or its first
  tick keeps failing, the harness runtime goes back to the last version that ran, as a new commit,
  and the user is alerted. A daemon that is alive but still in a slow first tick is left alone.
- Below `disk.min_free_gb` of free space no new worker starts, with one alert. Worktrees of tasks
  finished more than `disk.worktree_retention_days` ago are removed when clean and pushed or
  merged; branches are kept.
