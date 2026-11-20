---
"tt-project": patch
---

Daemon: a slow tick step can no longer livelock the tick loop, and per-tick cost no longer grows with
project history. A step cut off at its budget resumes where it stopped: the unreviewed-task sweep after
the task it stopped at, and finished probe verdicts (each of which can finish and review a task) are
kept for the next tick instead of handled all at once. Review ref lookups (named_refs, fix_branches)
narrow their rows in SQL, so a tick's queries and rows stay the same from 1,000 to 10,000 tasks (one
verdict tick at 10,000 tasks went from 1.66M rows and 6.8 s to the same cost as at 1,000). Three
overruns of one step within 10 minutes are one queued `tick_step_incident` naming the step, its overrun
count and its slowest call. `ttp config` accepts `watchdog.step_budget_s`.
