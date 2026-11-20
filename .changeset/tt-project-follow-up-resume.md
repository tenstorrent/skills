---
"tt-project": patch
---

Follow-ups resume their own worker's session: a continue hop, a daemon review fix, a rebase and a re-review start from a copy of the session of the last ok run of the task they carry on (or their own last ok run), never writing into it, with the coordinator's context since that run leading the prompt. A first review stays independent of its author's session. Where the session cannot be copied, the run falls back to the coordinator fork, then to the disk block; a rejected copy is not retried. The run note records `coord_context.how` `resume` with its source `run`, and `ttp stats --coord-session` shows per day how worker and review runs started (fork, resume, disk, none) and how many follow-ups resumed.
