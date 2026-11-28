---
"tt-project": patch
---

`tt-project`: the scheduler log (state/scheduler.jsonl) now says why ready work waits, and uses the same classifier as `ttp stats --ready-wait` (readywait.cause), so the two never disagree. A pass with every worker slot taken is logged too; before, it was never logged, so slot waits did not show. A closed budget gate, a gate that cut the slots, a task's own budget and the dollar caps log as `budget_gate` instead of `provider_gate` or `other`. A routed host's full slots log as `slot_cap`. Slots taken because an urgent reserve is kept stay `slot_cap` (a full hold), not a budget gate. The ready-wait sightings are kept in the project db and restored when the daemon restarts, so a wait already seen is no longer charged to `daemon_gap` by each restart.
