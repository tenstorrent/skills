---
"tt-project": patch
---

The failed-run hold stops only what keeps failing. Other tasks' waste holds a task only after it failed again (two wasted runs in the hour); one cheap failure holds nothing, and a failing loop source (schedule or harness) never holds its urgent or user-ordered tasks. The coordinator turn guard's event names the turns, their spend, the setting that tripped (`budget.hourly_coordinator_turns` or `budget.hourly_coordinator_repeats`) and the resume time; it holds only self-started coordinator turns, and workers and turns for your messages keep running.
