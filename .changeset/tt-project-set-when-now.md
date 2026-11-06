---
"tt-project": patch
---

`tt-project`: `ttp task set-when <id> now` (any case, surrounding spaces allowed) clears a waiting task's `retry_when` probe the same as `""`, as it already did for a `start_when` deferral, instead of storing the word as a shell probe that exits 127 and never passes.
