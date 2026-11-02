---
"tt-project": patch
---

`tt-project`: records kept in a task's name no longer outlive the task. Once a task is cancelled, failed, exhausted or done, has no live run and has been in that state two minutes, the daemon (every minute, model-free) removes its exclusive-resource reservations (`<lock>.reserved`) and asks each device runner to drop its `ttp devq` jobs: a queued one ends `cancelled` before it starts, a running one is stopped and ends `cancelled`, so `ttp devq probe` still answers. A job another open task's hand-off names is kept. `ttp devq submit` now records the job's project and keeps a local note of it under `state/devq-jobs/`; only jobs with both a matching project and task are dropped. Each release is a `task_record_released` event and one low alert.
