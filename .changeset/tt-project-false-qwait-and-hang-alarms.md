---
"tt-project": patch
---

Two false alarms no longer wake coordinators. The push queue-wait item counts a `ttp push` that never enters push_queue (a detached push): the live holder of the branch push lock shows as running with its step and minutes held, other running detached pushes as queued with their last log line, and no wait is raised while that holder lives within a push check's time limit; a dead holder still raises it. The checks queue incident times each holder against the median passing run of its own command list instead of the project's mixed median of full and targeted runs, so a full suite near its usual length is neither slow nor a likely hang; a command list with no passing run yet is not timed, and a likely hang still needs its log to have stopped growing.
