---
"tt-project": patch
---

`tt-project`: the queue trim (`queue.expire_h`) no longer expires a deferred task just after its not-before time passes: a task ages from the latest of its creation, its requeue and its `start_after`. Host sleep the daemon recorded counts as no queue age, like a project pause, and time both paused and asleep counts once.
