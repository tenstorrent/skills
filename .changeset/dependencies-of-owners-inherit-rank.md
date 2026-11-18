---
"tt-project": patch
---

`tt-project`: the dependencies of an obligation's owner or of an urgent task are dispatched with that rank, transitively, and a run whose supervisor process is gone is lost at once instead of after its lease goes stale. `replacegap.py` measures how long a lost runner's task waits for its replacement.
