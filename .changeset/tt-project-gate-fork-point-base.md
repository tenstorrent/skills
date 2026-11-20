---
"tt-project": patch
---

`tt-project`: the tests gate in `ttp checks` now reruns failing tests on the commit a head was built on, not on its merge base with the push branch. A task's worktree records its fork point when it is made, and the gate uses it while it is still an ancestor of the head; else the head's merge base with the branch the task is stacked on; else its merge base with the push branch. Before, a head built on a local batch or merge branch past the push branch had every failure already on that branch counted as new, so a change that added none was blocked. The check output and its excused record say which base was used and why.
