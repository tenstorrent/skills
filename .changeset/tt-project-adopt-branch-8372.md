---
"tt-project": patch
---

`ttp task <name> adopt-branch <id> <branch>` binds a task to a reviewed branch its saved work is on, with checks (no running user or pending push, a clean checkout, its saved head or built on its lineage, not another open task's branch) and a `branch_adopted` event; it changes only the task's branch. A task's worktree ownership now follows a review parent to the code task that review reviewed, so a worktree on that task's branch is adopted instead of stuck.
