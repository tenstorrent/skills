---
"tt-project": patch
---

`tt-project`:

- page severity: three daemon events that rarely need a decision no longer wake the coordinator as high every time. A done task's forgotten `continue:` follow-up (`continue_unqueued`) is normal and rides along with the next turn due anyway, at most an hour later; it stays queued, so a restart keeps it. A task blocked by the daemon or the push queue (`task_blocked`) is normal, and the same block again within 6 h is counted on the queued event or queued once to ride along. Both stay high for an urgent task (priority 1, an `urgent:` line, an open obligation's owner), and a block also while the task waits on an ask. An overdue machine change is normal, high only on a machine tagged `serving`, `live` or `server` past its end with no passing until-probe, and each project files it once per episode, whether from its own daemon or relayed by another project's.
