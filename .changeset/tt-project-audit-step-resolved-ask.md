---
"tt-project": patch
---

`tt-project`: the daily self-efficiency audit shows an ask that closed because its user step was done (`unblock.close_step_done`) as "resolved: #<task>'s user step was done, after <time>", with its open time ending when it closed, instead of "no answer yet" with an open time that keeps growing. `unblock.asks` exposes the step resolution (`step`: task and time), and the audit's ask count line adds how many asks closed this way. Asks closed by landing or by a linked answer keep their lines.
