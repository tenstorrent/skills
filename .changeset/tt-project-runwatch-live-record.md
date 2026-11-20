---
"tt-project": patch
---

`tt-project`: the daemon keeps `state/runwatch.json`, a model-free live record of each running worker and reviewer run, refreshed once a minute beside the spend meter. Per run it shows the last `ttp note` and `checks:` lines, the current or last tool call and how long it has been pending (read incrementally from the run's stream through a new provider `activity` method, for Claude and Codex), failed calls among the last 20, the files its worktree changed (from git, so shell-only edits show), spend against the task budget, the share of its wall limit used, its attempt, and whether it read the last update in its steer.md. `ttp runs live [--task N] [--json]` prints it, and the web app's running view shows the current call, changed-file count and last checks line.
