---
"tt-project": patch
---

`ttp leftover-procs --stop` and the daemon's leftover sweep never stop the project's live daemon, even when it carries a run's environment. They also end an ended task's `ttp checks` commands, attributed through their check-run folder's owner record, and a stopped process that leads a process group takes the group with it. If a member of that group is protected or belongs to another task, project or user, only the leader is stopped and the command says why.
