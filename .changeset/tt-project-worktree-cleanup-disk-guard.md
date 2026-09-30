---
"tt-project": patch
---

`tt-project`: A finished task's worktree is now removed as soon as the task ends, once it is clean and its
HEAD is on a branch (branches are never deleted, so `continues` still starts from them) and no unfinished
task (a review that pushes from it, say) still needs it. Git-ignored build and cache directories are
cleared even from worktrees that must be kept; untracked files that are not ignored are never touched, and `ttp prune <name>` sweeps on demand.
The disk guard now holds all but question and plan tasks when free space falls below the smaller of 5% of
the disk and 150 GB, alerts once per episode, lifts at 1.2× the threshold, and shows free space in status,
the coordinator digest and the web app.
