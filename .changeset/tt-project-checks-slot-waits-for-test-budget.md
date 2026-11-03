---
"tt-project": patch
---

`tt-project`: a `ttp checks` run (and `ttp push --own` checks) takes a host-wide checks slot only once its test slots could be taken (its project's test-worker budget and the host's test pool have room). Until then it waits outside the host queue at its own arrival time, so it no longer holds a host slot that only waits while another project's run could use it, and a bounded wait (the foreground hand-over, the spill-host bound) no longer takes and hands back the same slot every minute.
