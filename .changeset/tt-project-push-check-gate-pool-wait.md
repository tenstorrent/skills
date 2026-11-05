---
"tt-project": patch
---

`tt-project`: a push check's time limit no longer counts a tests gate's busy head or base rerun, or its backoff, as run time: both are recorded as a test-pool wait. A push check that exits 75 itself (no free slot, lock or pool, as `ttp checks` does when a gate rerun stays busy) makes the push answer busy (75) with a retry_when, never "checks failed" (4).
