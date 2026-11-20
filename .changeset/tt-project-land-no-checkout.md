---
"tt-project": patch
---

Harness landing never runs `checkout --` in the harness checkout: it moves the ref first (a refused ref update writes no file), then lets git update only files that still match the old HEAD. An uncommitted copy that already holds the merged content lands as it is; any other copy of a file the merge changes (other content, an earlier version, a deletion) stops the land and is named, so uncommitted, staged and untracked work there is never set aside or overwritten.
