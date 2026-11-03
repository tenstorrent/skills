---
"tt-project": patch
---

Continue chains: in a code hop's standalone clone (used where the project root is no git repository),
upstream commits the hop pulled or rebased onto during its run no longer count as its own progress:
the clone's upstream default branch (origin/HEAD, where it exists) is left out. The hop's own commits
still count, also once pushed to its remote task branch. The project root and worktrees sharing its
git directory keep their count.
