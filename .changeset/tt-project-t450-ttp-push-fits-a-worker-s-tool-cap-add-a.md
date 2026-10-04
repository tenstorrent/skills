---
"tt-project": patch
---

`tt-project`:

- ttp push --detach runs the push in its own process with a result marker and a --result probe
- tests skip git and file fsyncs a run's environment turns on
- a detached push removes its run lock when done; month-old markers are pruned
- two detached pushes started within one second get distinct markers
