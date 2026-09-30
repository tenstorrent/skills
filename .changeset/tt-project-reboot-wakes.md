---
"tt-project": patch
---

`tt-project`: a waiting task woken at boot because a reboot may have ended its job now counts that
wake against `budget.max_reboot_losses`, together with its runs lost to reboots, so a detached job
that keeps taking the host down gets blocked (high-severity event) instead of looping until
`max_waits`. The count starts over when the task is blocked, so a task that was requeued after a
reboot block is not blocked again by the next single loss.
