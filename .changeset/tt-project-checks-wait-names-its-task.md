---
"tt-project": patch
---

The checks queue incident (`Checks lane wait: ...`), `ttp status`'s checks lane wait lines and `ttp checks status` no longer call a task's checks run "a run with no task". A waiter that left the queue to rejoin it between the daemon's two reads of its ticket lost its task label; the waiter's task is now taken from one read of its ticket, else from the run it checks for, its environment or a parent's in its session, else its worktree. Only a waiter none of these link to a task reads as taskless, and then the line names its pid, command, working folder and first-queued time.
