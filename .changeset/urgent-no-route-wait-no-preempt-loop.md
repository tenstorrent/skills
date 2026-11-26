---
"tt-project": patch
---

`tt-project`: an urgent user-ordered task whose routed provider (the GLM arm) has no room now falls back to core on the first dispatch pass instead of waiting for a routed slot, so no run is preempted for it; a task preempted for urgent work is held while that task still waits and its preempted runs no longer count toward `chains.max_task_runs`.
