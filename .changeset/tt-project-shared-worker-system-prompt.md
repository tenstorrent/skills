---
"tt-project": patch
---

`tt-project`: a worker's and reviewer's appended system prompt (system.md) is now the same for every task of a project for a given charter and memory, so concurrent runs share its prompt cache. The worker.md rules that depend on the task (PR, wait, harness and cells blocks, and device or remote rules a task alone turns on) and the memory entries ranked by the task's words move to the task prompt, under a header that keeps them binding and names the section each belongs to. Restrictions stay in the system prompt verbatim. `ttp stats` now reports cold starts: the share of worker and reviewer runs whose first call read under 6 k cached tokens.
