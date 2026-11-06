---
"tt-project": patch
---

A code hand-off over `chains.split_lines` non-test lines now goes back to its worker once with a split order before any review: the same task, branch and worktree, asking for stacked parts. A hand-off that records `no_split` (why it cannot be split), an urgent task, a split part and a task already sent back once are reviewed at once; the review spec then notes the size.
