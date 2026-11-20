---
"tt-project": patch
---

`tt-project`: the coordinator skips a compaction that would free less than `coordinator.compact_min_free` (0.3) of its window unless the window forces it.
