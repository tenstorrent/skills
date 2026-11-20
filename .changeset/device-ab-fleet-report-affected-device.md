---
"tt-project": patch
---

Device A/B: a device or machine watcher's report stops a test arm only when it names the run's device as affected (beside a hang, reset, recovery, power-cycle or hold word, not as paused, healthy, checked or a box that cannot reach another) or cites the run's task or run id. A fleet report about other boxes that merely lists the run's device no longer stops the arm, and the next tick lifts a stop recorded under the old rule.
