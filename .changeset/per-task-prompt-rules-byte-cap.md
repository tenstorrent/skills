---
"tt-project": patch
---

A worker's prompt now carries less of the charter and has a size cap. Each Goals or Resources section of the worker digest shows at most the digest's budget; blocks past it come in the task prompt only of a task that shares their words. `worker.prompt_max_bytes` (default 60000, 0 off) caps system.md and prompt.md beyond the binding restrictions and the task: past it the memory block, the context block and the task rules lose their last lines in that order. Forked runs are exempt. Memory pointers past the first twelve are one count line, and the daily review gives the median prompt bytes per role and the runs over the cap.
