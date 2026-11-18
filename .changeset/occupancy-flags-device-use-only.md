---
"tt-project": patch
---

`tt-project`: the occupancy watchdog flags only jobs that used a covered box's own devices (a broker job on it, a lock on its device resources or a device or reset step), read from the job's structured fields rather than its command, so CPU checks, test suites and host processes that drive other hosts are no longer flagged. A job that started before the stream went live is never flagged. One alert per stream event (box plus window start) lists the first 10 jobs and "+N more" and carries the source's own reason at the hit, so a real clip and a fail-safe hold with no owner shown read differently; per-job flags stay in the project's state for status.

A model-free reaper (`activity.reap`, run on every watchdog pass, so also at daemon start) closes every activity record whose process is known gone on this host (job lock free, pid dead, another boot) with an end marked `reaped`; a reaped record never counts as live for the watchdog, and a job whose end is known only on another host (`ttp detach --remote`) counts by its start inside the live window alone. The roll-up alert is high severity only when the source reported a real live stream, and normal when the box only counted as live because the source could not tell; seen windows never alert again, across restarts.
