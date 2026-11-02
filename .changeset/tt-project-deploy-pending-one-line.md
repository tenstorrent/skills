---
"tt-project": patch
---

`tt-project`: a pending deploy is one low status line per project (`deploy-pending:<name>`, e.g. "alpha will switch to tt-project 0.5.2 by <time> (it waits for harness task #12 to finish)."), updated in place by each newer release instead of a new feed line per release. It clears with a short line once the project runs the release, and quietly when its check fails (the existing deploy-behind alert tells that).
