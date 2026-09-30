---
"tt-project": patch
---

`tt-project`: a waiting task whose `retry_when` probe still exits 1 when its timer runs out now
sleeps another `retry_after_s` instead of starting a worker run just to wait again. It wakes when
the probe exits 0, when the probe is broken (any other exit, a timeout, cannot start), or
`waiting.max_hold_s` (default 6 h) after the hand-off, and the resumed worker is told which. After
a daemon restart the probe runs before the task wakes; a requeue from the coordinator or the web
app runs it without the hold. The worker prompt asks for probes that exit 0 once the wait is over
whatever the outcome, and for multi-step waits chained in one detached driver.
