---
"tt-project": patch
---

`tt-project`: a task that waits more than 30 minutes (`coordinator.queue_wait_escalate_min`) on the checks queue, the push queue, worker slots, a device or lock, an outside batch pool or CPU admission now becomes a must-act item for its coordinator. The item names the queue, its cap, who holds and waits for each place, and unblock options computed from config and host state, leaving out any route that a user pause or a user-set cap forbids. The coordinator must queue a fix or name the rule that holds it in that turn. If it does neither, the item comes back 30 minutes later. When only the user can clear the wait, the user is told once. The daily review shows `queue_wait_over`: the waits, their max and p95, and the time to the first unblock action.
