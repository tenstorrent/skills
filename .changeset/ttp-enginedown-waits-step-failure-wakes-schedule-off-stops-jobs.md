---
"tt-project": patch
---

`tt-project`: the Terminal-Bench slice runner waits for an unreachable docker engine with a bounded, configurable backoff, re-probes it and resumes the row, ending the row only after every wait with a clear reason. A live `ttp detach` driver whose step fails (a progress report failure) wakes the task waiting on its `ttp detach --check`, once per batch; a healthy live driver still skips. Turning a command schedule off now also stops the background jobs its runs left running (marked `TTP_SCHEDULE`, or for jobs started before the mark, matched by the schedule's `ttp` command with no task), through ttp's audited stop path, on the next tick; a schedule already off is rechecked every minute. The Terminal-Bench launcher records each runner's process group, so `ttp detach --stop <schedule>` finds it, and the daemon stops this project's runners (recorded, or older ones proven to be its own) while the bench is held, off or stop-switched.
