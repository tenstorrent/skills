---
"tt-project": patch
---

`tt-project`: fixes found by dogfooding on another project.

- Claude Code worker runs can no longer start background tasks, which were killed when the run
  exited and lost the job with no hand-off. The worker prompt says to detach long jobs.
- Approved worker plugin folders load: `providers.claude.plugin_dirs` accepts a JSON list or a
  comma-separated string, rejects folders that do not exist, and alerts when a configured folder
  is missing instead of dropping it silently.
- `ttp status` and the web app say when open questions have not reached any chat for 10 minutes.
- A task's reason no longer goes stale: coordinator notes on tasks that are not blocked or
  cancelled go into the spec, and a finished run clears an old reason.
- The web app shows every open question, not only high-severity ones.
- Running workers' spend so far is priced every minute, shown in status and the web app, and
  counted in the daily and weekly caps and the hourly guard.
