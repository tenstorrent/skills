---
"tt-project": patch
---

`tt-project`: an `exclusive:<resource>` task that finds every slot held by `ttp lock` commands
reserves the resource, so new `ttp lock` commands wait until it has started instead of starving
it. A reservation nobody refreshes lapses after two minutes, so a crash never wedges the resource.
The run's wait for its slot has its own bound (`budget.exclusive_wait_s`, default 600 s) and the
run's wall clock starts once it holds the slot; a run that gives up goes back to the queue with no
attempt spent.
