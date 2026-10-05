---
"tt-project": patch
---

`tt-project`:

- push queue tables and pushq module (WIP)
- push queue daemon hooks, alerts, delivered check and status entries (WIP)
- push queue tests (approval, scheduling, finalize outcomes, edges, migration)
- push queue: a push list while the queue is off sends the review back to push itself; tick step test counts the push steps
- push queue tests check pushq.summary (approved rows, live batch, last batches)
