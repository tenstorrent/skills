---
"tt-project": patch
---

`tt-project`:

- the worktree sweep reads the push markers once per sweep, not once per worktree, and ended markers are not re-read while unchanged
- the duplicate scan keeps each text's word set and skips spec words when titles differ; a git check (check-ref-format, var, version) gets a floor timeout and is not refused by a round's slice
