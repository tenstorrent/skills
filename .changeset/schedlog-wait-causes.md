---
"tt-project": patch
---

`tt-project`: the scheduler log (state/scheduler.jsonl) now says why ready work waits. A pass with every worker slot taken is logged too; before, it was never logged, so slot waits did not show. A closed budget gate (spend level, runaway guard, plan window) logs as `budget_gate` instead of `provider_gate`. A routed host's full slots log as `slot_cap`. A task's own budget or the dollar caps log as `spend_cap` instead of `other`.
