---
"tt-project": patch
---

`tt-project`: a spill host no longer gets the pinned checks env built from its package index unless the project sets `checks.spill_install` to `true` (default `false`). While it is off, no pip runs there: a check that reaches a spill host without a ready env of the same lock and interpreter does not run there: it faults and reruns elsewhere or here, the host leaves that project's spill pool for 30 minutes (other projects are not affected) and `ttp status` shows `no ready checks env; install off (checks.spill_install)` on its host line. The health check never fails a host for a missing env while it is off. Hosts with a ready env, check hosts and projects that opt in work as before.
