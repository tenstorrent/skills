---
"tt-project": patch
---

`tt-project`: a task held behind another by its lineage label (`waits_end`, set when it shared a branch or worktree with an open task) is released on the next dispatch pass once it no longer shares a branch or worktree with that task, for example after its `pr_branch` moved. A task on the same branch or with an overlapping worktree stays held. `ttp status` keeps `deferred until <time>` on an urgent row whose deferred row falls past the five it prints. The web app reads a task's start time before cutting its result to a summary.
