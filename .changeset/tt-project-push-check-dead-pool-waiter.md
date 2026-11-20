---
"tt-project": patch
---

`tt-project`: a push check's time limit no longer counts a test-pool or grant wait as run time after the waiter dies mid-wait without closing its record (a killed shard's `ttp pool-wait`). The push keeps every wait it has seen, so counted run time cannot jump past the limit between two polls, and the "ran past its limit" line gives the same counted seconds the decision used.
