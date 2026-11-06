---
"tt-project": patch
---

`tt-project`: the guide documents how long an urgent checks run that preempted a routine run while it waits for its test slots keeps its claim on the freed slot (up to 60 s from its ask, so a run slower than 30 s to end its command still hands the slot to it), with a test of that case.
