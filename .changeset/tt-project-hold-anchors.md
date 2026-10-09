---
"tt-project": patch
---

A coordinator hold needs an anchor and an end. `task_update` with status `blocked` must carry `waits_on`: `ask:<id>` (or `ask:new` for this turn's ask_user), `resource:<name>`, `until:<time>` or `when:<probe>`, kept as the task label `waits:<kind>:<value>`; a hold without one is rejected with what to do instead. The daemon requeues an anchored hold model-free once its ask is resolved or expired, its time passes, its resource is free and unpaused, or its probe exits 0. Blocked tasks with no anchor (legacy holds and daemon or worker blocks included) held past `coordinator.hold_max_h` (default 12) or older than a newer user message raise one high-effort 'stale holds' coordinator trigger that lists them, again only when that set gains a member. The daily review's unblock lines count holds with no anchor and their ages, and the coordinator prompt says a hold never replaces an ask or a decision.
