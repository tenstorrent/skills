---
"tt-project": patch
---

`tt-project`:

- old user orders are read by intent: `memory_add` and an order `obligation` keep the coordinator's `reason` and the cited user message's own words (`context`) beside the order. A model-free scan (`oldorders.py`) lists standing rules, preferences and open orders older than a day whose reason is over (every task it names finished) or unknown, with the holds that cite them, under 'Re-read by intent' in the coordinator digest once a day and on each blocker sweep; Course check flags a blocked task, deferral or ask that cites one (`coordinator.course_check` `old_orders`). Restrictions are never listed. One coordinator prompt line: retire an order whose reason is gone and say so; tests
