---
"tt-project": patch
---

`tt-project`: the occupancy watchdog flags only jobs that used a covered box's own devices (a broker job on it, a lock on its device resources or a device or reset step), read from the job's structured fields rather than its command, so CPU checks, test suites and host processes that drive other hosts are no longer flagged. A job that started before the stream went live is never flagged. One alert per stream event (box plus window start) lists the first 10 jobs and "+N more" and carries the source's own reason at the hit, so a real clip and a fail-safe hold with no owner shown read differently; per-job flags stay in the project's state for status.
