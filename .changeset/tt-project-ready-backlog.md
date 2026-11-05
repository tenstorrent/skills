---
"tt-project": patch
---

`tt-project`: the ready-not-started check tells a backlog from a stall. While more tasks are ready than their provider has free slots, or the spend gate stopped new work within the last hour (the queue a red gate leaves), a late task with no recorded hold raises no alarm; one handled `ready_backlog` record per episode, and a daemon log line, give the queue depth, when the gate cleared (or that it still holds) and why, the live runs and the free slots. Deadline work (an `urgent:` line or an obligation owner) keeps its alarm. With free slots and no recent gate hold, the plain alarm is unchanged; a task held by a backlog counts its wait again from when the backlog drains.
