---
"tt-project": patch
---

`tt-project`: a task can end the `ttp detach` jobs its earlier runs started. Each job's registry entry
now names its project, task and run; `ttp detach --list` lists the calling task's jobs from all its
runs, and `ttp detach --stop <name>` takes a name an earlier run used. The worker kill guard lets the
task signal such a job's group once the project db, the job's run registry, the pid's start time and
the job's own environment agree; another task's job, a forged registry entry, an owner file under
project state and a recycled pid stay refused.
