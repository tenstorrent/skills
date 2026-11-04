---
"tt-project": patch
---

`tt-project`:

- When the budget gate allows fewer worker slots than `budget.max_parallel_workers` (yellow under the dollar caps halves them, orange allows one, plan headroom sets its own number), the slot-wait observation now says so, e.g. "all 7 are taken (12 configured; the budget gate allows 6 at yellow: 63% of cap used; +1 urgent)", and names the slot kept back for urgent work. Before, it said only "all 7 are taken", which sent a coordinator looking for a host or provider limit. An urgent task held by the spend gate names the gate's level and cause. `ttp status` and `ttp overview` show a line per provider whose gate cuts its slots.
