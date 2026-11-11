---
"tt-project": patch
---

`tt-project`:

- a checks run waiting here keeps trying the spill hosts and a host its own faults kept off is tried again once the checks config changes or after 10 min
- a run that failed tests on a spill host reruns only those tests, here or on another spill host, and their failing again counts
- the spill probe counts only checks a live process runs and removes a cut run's leftover folder
