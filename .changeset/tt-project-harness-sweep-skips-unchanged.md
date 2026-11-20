---
"tt-project": patch
---

The daemon's harness-worktree landing pass no longer runs git on every kept worktree each tick. A
worktree a pass kept, or found conflicting, is skipped while its task row, its run folders and the
HEAD of it and of the harness checkout are unchanged, and is looked at again at least every five
minutes. With 20 kept worktrees a pass went from 82 git spawns to none.
