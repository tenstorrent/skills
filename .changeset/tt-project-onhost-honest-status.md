---
"tt-project": patch
---

`tt-project`: a coordinator reply or notify no longer reports a machine or device action as under way or done before a worker has started it on the host. Each task run records when it first gets a lock on a resource its task names (or runs its first remote command); digest rows and `Expected finish` show 'queued for a worker, ETA <start>', 'worker running, waiting for <resource> since HH:MM' or 'on <resource> since HH:MM'. A sentence claiming an action for a task in scope (queued in the same batch, the reply's task, or named) that has not started is still sent, marked 'Planned, not started on the host yet:', with the honest status appended, and the coordinator gets a note. A cancelled task whose detached jobs still run reads 'stop requested; N jobs still running on <resource>' until the job sweep sees them end. The coordinator prompt states the rule.
