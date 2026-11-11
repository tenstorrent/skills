---
"tt-project": patch
---

`tt-project`:

- a review waits for the pre-checks the daemon started on its head only while those checks run on a host (`checks.running`, written once they leave every queue), never while they wait for a host checks slot, a lane or another run of the same checks. The wait lasts at most the median of the project's recent checks runs (capped at 1 h, which is also the limit before any run is measured). A priority-1 or `urgent:` review, or one that owns an open obligation, is never held: it starts at once, judges the diff, runs focused tests and reads the pre-checks result only if it is already there. The pre-checks keep their queue place and still feed the push queue's reuse. The daemon, the overseer and the throughput report read the same hold; tests
