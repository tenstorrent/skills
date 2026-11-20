---
"tt-project": patch
---

`tt-project`:

- project hand-off checks: `worker.handoff_checks` lists the project's own model-free commands the daemon runs on each worker's hand-off before it accepts it, posts its chat reply or queues its review or push. Exit 1 sends the run back with the check's output (at most `worker.handoff_check_max_sendbacks`, default 2, then the task goes to review with its result kept); any other exit or a timeout accepts the hand-off and raises one `handoff_check_broken` event per check.
