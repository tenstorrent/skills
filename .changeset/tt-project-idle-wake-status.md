---
"tt-project": patch
---

`tt-project`: "next idle check" in `ttp status` and the web app is the time the daemon will
really wake the coordinator.

- The daemon and the status view share one idle-wake rule, so the shown time includes the
  doubling after unchanged wakes and the earlier idle-slot wake on plans.
- When the budget gate holds optional work, status says the idle check is held by the gate
  instead of showing a time that never comes.
