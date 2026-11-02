---
"tt-project": patch
---

`tt-project`: each Codex run now checks that its rollout recorded the system prompt it passed with `-c developer_instructions` (a developer message in the rollout's first 200 lines starting with the run's system.md, compared over 4 KB). A task run whose rollout lacks it is stopped, and its task is blocked without spending an attempt, since the next run would not take it either. One observation names the Codex CLI version and is raised once per version, not per run. A rollout that does not appear within 120 s only raises one observation; the run goes on, as the session file's path or format may change.
