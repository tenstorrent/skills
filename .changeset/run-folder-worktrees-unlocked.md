---
"tt-project": patch
---

`tt-project`: the worktree sweep no longer locks git worktrees that workers make in their run folders (state/runs/<id>/...). Only task worktrees under tt-project/worktrees/ get the keep lock. A run-folder worktree an older sweep locked has that lock lifted at the next sweep, so its worker or anyone else can remove it with a plain `git worktree remove --force`, and a run that ends without cleaning up no longer leaves it behind locked. The sweep still keeps a live run's or an open task's worktree.
