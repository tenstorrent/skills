---
"tt-project": patch
---

`tt-project`: the idle daemon tick reads less of a long-lived project's database. Dependency checks read only the tasks depended on. New indexes cover open tasks, the last task update, the last inbound message, open asks and runs by end time. Within a tick, the spend checks (last hour, today, this week, in flight, global cap) share one read of plan readings and runs, parsed once, until any write to the database. Behaviour is unchanged.
