---
"tt-project": patch
---

Tier floor for conclusion runs: question and plan tasks, chat answers, specs with a `conclusion:` line and reporting llm schedules (the daily review, summaries) start at standard tier and high effort or above, lifted at dispatch after the effort pick so queued tasks are lifted too; `coordinator.conclusion_floor` sets it per project. The coordinator prompt keeps light for mechanical work only. A conclusion task's wake whose wait is over runs at the floor, and a budget gate below the floor holds a conclusion run queued instead of clamping it to light.
