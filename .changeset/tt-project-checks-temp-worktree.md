---
"tt-project": patch
---

`tt-project`: `ttp checks` records the checked worktree by its real path, so a later start never goes through a link. Detached checks of a worktree inside the run's temporary folder (its TMPDIR, removed when the run ends) are refused up front, as is handing busy foreground checks there over to detached ones: before, they queued and then died once the run's TMPDIR was gone. The message names a lasting place in the run dir and the `git worktree move` that keeps uncommitted changes. The worker prompt says to put extra worktrees in the run dir, never under `$TMPDIR`.
