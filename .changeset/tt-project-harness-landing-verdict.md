---
"tt-project": patch
---

`tt-project`: a project's hand-off check for harness tasks now gets the daemon's own verdict on whether the task's commits land, in `TTP_HARNESS_LANDING` (`merged`, `lands` or `refused`) and `TTP_HARNESS_LANDING_WHY`. A commit on the task's branch that fast-forwards the harness HEAD reads as `lands`, since the daemon merges it when the run ends. Before, a check could only test the harness HEAD, so it sent the worker back to merge into the harness checkout, which the worker's sandbox cannot write. A branch that does not fast-forward, or a named commit the harness does not have, reads as `refused`, with steps the worker can take (rebase in its own worktree), never a merge into the checkout.
