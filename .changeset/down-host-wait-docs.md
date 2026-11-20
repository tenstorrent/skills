---
"tt-project": patch
---

Document in the guide that a wait on a down ssh host is re-probed by the daemon (15/30/60/120 s), so
workers write no poll loop for it, and what recovery the daemon runs once per down episode (only for
a machines.json alias: the recovery owner's note, a blocked fix logged, the heal fix, or one
`host_down` event).
