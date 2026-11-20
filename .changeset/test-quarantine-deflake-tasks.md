---
"tt-project": patch
---

`tt-project`: flaky tests cost fewer model runs. Tests the tests gate quarantines on one day (home zone) now go into one deflake code task per test module, written by the harness without a model call, in place of a coordinator note per flaky test (and the coordinator turn, task, premise check and review that each note led to). The task is held until the next day starts so the rest of that day's flakes of the module join it; a task that already started is never rewritten. Once the deflake task lands on the push branch, the daemon's model-free sweep lifts every test it lists, also when its title is cut; the lift still needs a landed fix named for that exact test.
