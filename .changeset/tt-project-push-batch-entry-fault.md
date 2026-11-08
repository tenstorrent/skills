---
"tt-project": patch
---

A push batch no longer fails as a whole when one entry's replay raises. The imports rule of the conflict settler crashed with `IndexError` when the tip's side of an import conflict held only comments, which failed every batch that carried that entry and landed nothing. That side now counts as no import statements, so the entry goes back as an ordinary conflict. Any other unexpected error while one entry is replayed now sends back only that entry, as a conflict naming the error (one conflict fix), resets the batch worktree, and lets the rest of the batch go on. The same error in every entry so far, a worktree that cannot be reset, or a host fault still fails or holds the whole batch as before.
