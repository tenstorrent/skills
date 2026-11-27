---
"tt-project": patch
---

`tt-project`: `ttp timeline <project> <task>` tells, model-free, where one task's time went: creation and queueing, each run's dispatch, worker start and end, each detached job's start, end and exit code (a remote job from the wait record that polls it), and each wait with the wake that ended it. Wall time is totaled by bucket: queue, start-up, test (detached jobs), wait, model, with failed-and-redone time called out.
