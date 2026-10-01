---
"tt-project": patch
---

`tt-project`: A run that wakes a waiting task now runs at the light tier when the hand-off named a
`retry_when` or `waiting_for`, unless it set `wake_tier` (never above the task's own tier). A light
wake that finds real work hands off `waiting` with `retry_after_s: 0` and a higher `wake_tier`, and
runs again at once at that tier, once per wake and without costing an attempt or a wait. A lost wake
retries at the task's tier. `ttp status` and the web app show a run's wake tier.
