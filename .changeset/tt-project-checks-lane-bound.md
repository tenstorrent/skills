---
"tt-project": patch
---

Checks lanes: a run takes a lane only from the first lanes its count allows, and recounts the held lanes under a per-repository guard before it takes one, so an out-of-date free count never admits more check suites than the bound.
