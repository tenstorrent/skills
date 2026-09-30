---
"tt-project": patch
---

`tt-project`: A finished task's worktree is now removed soon after the task ends, once it is clean, its
HEAD is on a branch (branches are never deleted, so `continues` still starts from them), it has no submodules
set up (their commits may exist only in that worktree, so such a worktree is always kept), and nothing may
still want it: no unfinished task (a review that pushes from it, say) depends on, continues or names it, the
coordinator has seen how the task ended, and the task ended at least an hour ago.
`disk.worktree_retention_days` keeps its old meaning for 0 (never tidy); its default is now null (no delay
beyond that hour), and a positive value waits that many days. Git-ignored build and cache directories are
cleared even from worktrees that must be kept; untracked files that are not ignored are never touched, and `ttp prune <name>` sweeps on demand.
The disk guard now holds all but question and plan tasks when free space falls below the smaller of 5% of
the disk and 150 GB, alerts once per episode, lifts at 1.2× the threshold, and shows free space in status,
the coordinator digest and the web app.
