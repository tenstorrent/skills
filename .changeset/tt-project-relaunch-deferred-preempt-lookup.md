---
"tt-project": patch
---

After a reboot, the daemon no longer starts checks again (relaunch-t<task>) for a task that is deferred
(start_after still to come, or start_when not yet met), cancelled or done: the task stays asleep and runs its
checks when it next starts, so they hold no host checks slot owed runs wait for. `ttp checks --preempt <run>`
now also finds a run by the run folder `ttp checks --slot-status` names, so a relaunched run can be preempted.
