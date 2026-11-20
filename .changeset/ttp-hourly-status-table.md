---
"tt-project": patch
---

`tt-project`: one model-free status table. `ttp status --hourly` prints the fork rate per provider (last hour and today, against the new `review.fork_target`, default 0.9), the project host's load per core (sampled every 4 minutes by the overseer and the daemon into `state/host-load.jsonl`) and each spill host's use in 24 h (runs placed, passed, run again on the project host), test runs, CPU-hours and $ per landed change, the queue, and the approved-to-landed median with tasks created in 24 h. The daemon posts it to the main chat at most hourly or at a milestone (a batch landed, a deploy, an order closed), and only when a row changed; `notify.hourly_status` turns it off. `ttp checks` records which spill hosts a run was moved off, so the table can count reruns.
