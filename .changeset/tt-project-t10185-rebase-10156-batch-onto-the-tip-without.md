---
"tt-project": patch
---

`tt-project`:

- ack-wait: a request closed by its parenthesized id reads as closed
- partial clone: blob and commit reads never lazy-fetch one object at a time
- worktree sweep: a finished worktree whose submodules lose nothing is freed
- landed verdict reads lazily again; worktree sweep keeps a worktree whose submodule it cannot read
