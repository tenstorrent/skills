---
"tt-project": patch
---

`tt-project`: dispatch re-reads each task's row just before it starts a run. A task that was cancelled, closed done or failed after the tick's ready list was read is not started. A task that was closed done as many times as its attempt limit since its last explicit requeue is not started either, even if a write put it back in the queue: dispatch puts it back to done, logs `task N not dispatched: ...` and records a `dispatch_refused` event. Explicit requeues (`ttp task requeue`, the web app, the coordinator's status queued, a chain continue, the push queue's review re-runs) record a `task_requeued` event, so the count starts again after each one.
