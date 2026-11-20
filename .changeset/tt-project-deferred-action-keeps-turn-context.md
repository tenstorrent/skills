---
"tt-project": patch
---

`tt-project`: a coordinator action whose git check timed out is no longer rejected: a task_add or task_update that wrote nothing yet is kept and applied again on the daemon's next tick, rejected only after repeated timeouts. Branch names are checked in-process, so the coordinator's branch checks start no git. The replay keeps the action's turn context: a task_add citing a user message of its turn is still recorded as the latest user order on its resources (older conflicting tasks held, running workers told), and a task_update holding a task on `waits_on ask:new` waits on the ask its turn sent, or is rejected at once when no ask of that turn went through.
