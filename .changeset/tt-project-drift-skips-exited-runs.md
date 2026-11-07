---
"tt-project": patch
---

The live drift check no longer flags a run that has already exited but is not reaped yet. A run counts as ended once its exit.json is written, its stream holds the provider's final event (Claude Code and Cursor `result`, Codex `turn.completed` or `turn.failed`), or its lease is stale and its recorded runner no longer runs. Such a run gets no flag, no steer.md update and no worker_drift event: its final spend, booked at its result, used to look like a fast pace.
