---
"tt-project": patch
---

`tt-project`: a spill host no longer gets the pinned checks env built from its package index unless the project sets `checks.spill_install` to `true` (default `false`). While it is off, no pip runs there: a spill host without a ready env of the same lock and interpreter fails its health check with `no ready checks env; install off (checks.spill_install)`, stays out of the project's spill pool (never counted as a config fault), and `ttp status` shows that reason on its host line; a check that reaches it anyway faults and reruns elsewhere without marking the host down for other projects. Hosts with a ready env, check hosts and projects that opt in work as before.
