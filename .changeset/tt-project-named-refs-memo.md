---
"tt-project": patch
---

`tt-project`:

- daemon: a finished task's named review refs are remembered for 15 minutes; recomputing those of every earlier review on each dispatch and review sweep (a task-table scan plus git ancestry calls each) ran every tick step past its budget on a project with thousands of tasks, so nothing was dispatched or reaped; tests
