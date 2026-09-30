---
"tt-project": patch
---

`tt-project`: On a plan whose window is on pace to overshoot, deep tasks now run at the standard tier
(orange and red are unchanged), and a project already down to one worker is paced further: when the
pace allows only a fraction `f` of one worker, the next new start waits the last run's length x (1/f - 1)
after it ended, at most `budget.max_pace_hold_s` (default 7200 s). The user's own tasks, tasks answering
a chat and reviews start anyway; running work is never stopped, and a hold wakes no coordinator.
`ttp status` and the web status line show `paced: next start ~HH:MM (<window> on pace for N%)`.
