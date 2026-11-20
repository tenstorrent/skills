---
"tt-project": patch
---

`tt-project`: before task_add or the daemon's continue hop chains a code task on `landed:#<id>` for a file or symbol the base branch lacks, it now checks the commit the task will start from: the branch it carries or rebases, or the branch of the task it continues. The chain is skipped when that commit has the file or symbol, or already contains the other task's head. A fresh task off the base branch is chained as before. Only cheap local git reads are used, and a start commit that cannot be read falls back to the base-branch check.
