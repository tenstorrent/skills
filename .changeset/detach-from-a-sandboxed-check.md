---
"tt-project": patch
---

`ttp detach` run by a push check or `ttp checks` command inside the check's process sandbox no
longer dies with the check: the job is queued and the daemon starts it outside, keeping the
check's device masking. It ends 6 hours after it was queued or once the push or checks run that
owns the check is gone, whichever comes first.
Any `ttp detach` run in a PID namespace of its own is queued the same way, and a job's record now
holds its host pid and start time: a recycled pid counts as gone, never signalled.
