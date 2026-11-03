---
"tt-project": patch
---

Checks: a `ttp checks` run that re-executes itself after a runtime upgrade keeps the host-wide checks
slot it was already granted (with its checks lane), instead of handing it back and queueing again
behind later arrivals. The slot's lock file stays open across the exec, so no other waiter can take
it in between; a failed exec or a dead process frees it as before. A run still waiting queues again
at its first arrival and priority, so its ageing counts from then.
