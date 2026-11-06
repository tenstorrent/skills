---
"tt-project": patch
---

A new coordinator session's first wake gets a per-wake dollar cap that also covers its seed: the
dearest of the last few new-session wakes, or without any, the seed prompt's size at the model's
prices. The raise stays within the project, account and hourly coordinator caps and never lowers the
per-wake cap; resumed wakes keep their own cap. Before, a seed over the per-wake cap failed with
error_max_budget_usd and the next wake paid for the seed again.
