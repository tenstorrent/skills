---
"tt-project": patch
---

After a host reboot, a task whose retry_when gates on a detached job's .rc path in any form (`test -s <rc>`,
`test -e <rc>`, a run-folder probe script given the path, `bash -c '...'`) is treated like one using
`ttp detach --check <rc>`: its resumable job is relaunched and the task stays asleep, instead of waking
as due now. A probe naming an unrelated path still does not relaunch anything, and a gate script that also
probes a process or a /tmp path still wakes the task.
