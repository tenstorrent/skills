---
"tt-project": patch
---

The runaway guard's waste signal holds the task that wasted, never the project. A task whose failed or stalled runs in the last hour pass `budget.hourly_waste_usd` (usage billing, default $8) or `budget.hourly_waste_runs` (plan billing, default 4) waits until its oldest counted run is an hour old and the coordinator gets one event for it; new tasks failing one after another hold those tasks and their loop source (schedule or harness) the same way. The gate level and the other tasks are untouched. `ttp status`, the web app's Budget tab and the coordinator digest list each held task with the setting and its resume time. The total hourly ceiling and the dollar caps still hold the whole project.
