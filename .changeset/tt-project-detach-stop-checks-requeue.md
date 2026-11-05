---
"tt-project": patch
---

`tt-project`: `ttp detach --stop <run>/checks.rc` (or `checks` from that run) now stops a run's detached `ttp checks`. It uses the same rule as a detached job: the owning task, an ended task or a priority-1 resource holder may stop them. A group that holds the caller, a running worker, another task's or another project's process is refused, and nothing is signalled. The daemon also stops the detached checks of a task that handed off done, after a grace period, unless the task's own hand-off, an open task or a joined checks run still waits on their result. A detached job can now list processes it re-spawned outside its own group in `<TTP_DETACH_RC>.requeue-pid`, one `<pid> <start ticks>` line each. `ttp detach --stop` and `ttp detach --stop-pids` stop those groups too, but only while the start ticks still match. A missing file, a malformed line, an exited or reused pid, and another project's process are skipped.
