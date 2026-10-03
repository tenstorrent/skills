---
"tt-project": patch
---

`tt-project`: A waiting task's `retry_when` probe that exits 255 (ssh could not reach the host)
now counts as "not yet", like exit 1: the task stays asleep without a worker run, still bounded by
`waiting.max_hold_s`, and the daemon logs it as host unreachable. Other exits and timeouts still
wake the task as broken. The worker prompt now says how to wait on a job on another machine: start
its detached driver there, keep the marker there, probe it over ssh and set `survives_reboot`.
