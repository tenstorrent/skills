---
"tt-project": patch
---

`tt-project`: An llm schedule skipped because the budget gate holds optional work no longer loses
its period. It keeps its last run, retries every 30 minutes (or its own period, if shorter) and runs
once the gate allows it. `ttp status` and the web app's schedules list show it as waiting for budget.
Other skips (nothing happened, previous run still open) still wait a full period.
