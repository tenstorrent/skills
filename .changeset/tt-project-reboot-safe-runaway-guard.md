---
"tt-project": patch
---

`tt-project`: runs cut short by a host reboot, and lost runs whose hand-off stood, no longer count
as runaway waste, so a reboot no longer holds the budget gate at red. Their spend still counts
toward the caps and the task's budget. After a reboot the daemon posts one notice listing the lost
runs, their cost and what became of their tasks. A passed `retry_when` probe that the gate holds is
now logged as held, not as dispatching.
