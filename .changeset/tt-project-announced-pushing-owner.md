---
"tt-project": patch
---

`tt-project`: the announced-not-started check no longer raises `announced_not_started` ("pushing without starting") for a promise owner the push queue is delivering. An owner in status pushing counts as started while the queue is alive on it (the batch holding it runs, or it waits in a queue that is not stalled); a dead batch or a stalled queue holds it, and the queue's own events (`push_batch_died`, `push_queue_dying`, `push_not_landing`) page it instead. A pushing owner with no open queue row is still raised.
