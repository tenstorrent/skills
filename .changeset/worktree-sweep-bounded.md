---
"tt-project": patch
---

The daemon's worktree sweep bounds each git call and its whole pass: a hung git keeps its worktree and the rest of the sweep moves to the next tick, so it can no longer stall the daemon loop. `ttp prune` is unchanged.
