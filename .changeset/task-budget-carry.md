---
"tt-project": patch
---

A task budget never stops a tested landing. `task_update` with a higher `budget_usd` now applies to a running task at once: runs on providers the runner budgets reread the new limit, and a run whose agent cap was fixed at launch goes back to the queue with no attempt spent. A code task whose run stops on its budget after `ttp checks` passed on its branch head is handed off as done by the harness, with the checks as evidence, so review and the push queue follow; the overrun (budget, spent) is recorded as a low event and a follow-up. A run without such a pass still stops at its budget, and the dollar caps stay hard limits.
