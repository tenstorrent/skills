---
"tt-project": patch
---

tt-project 0.2.1003: on macOS the daemon's job and push stop can now read a process's arguments, process group and environment. macOS has no /proc, so `jobstop._argv` returned nothing and a cancelled task's push was never stopped. A new `procinfo` module reads arguments and environment with sysctl KERN_PROCARGS2 (only this user's processes) and the process group and owner with `ps`. jobstop's `_argv`, `_stat`, `members` and `_environ` and the kill guard's environment check use it when there is no /proc. The safety rule is unchanged: a pid is signalled only while its arguments still carry the job's marker or .rc. On macOS `group_of` now also checks that, where before it trusted the pid. `ttp detach --stop-pids` and `ttp leftover-procs --stop` stay Linux-only for now.
