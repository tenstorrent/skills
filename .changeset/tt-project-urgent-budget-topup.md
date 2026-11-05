---
"tt-project": patch
---

An urgent or user-ordered task no longer waits on its spent per-task budget: the daemon raises it
by the tier's default and continues the task at once, without a coordinator turn. Each top-up is
recorded on the task and sent as one low notice, and status, the web app and the digest show it.
The dollar caps still stop it, and a runaway guard fails a task whose runs keep stopping on their
budget with no hand-off and no new commit. Other tasks keep the budget hold.
