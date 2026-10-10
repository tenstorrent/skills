---
"tt-project": patch
---

`tt-project`: a re-review covers the newest fix branch that builds on the branch it reviews (a
re-plan that could not check that branch out commits on its own `ttp/t<id>-...` branch). The push
queue accepts the head the reviewer names there, only when it contains the reviewed branch, so the
re-review no longer needs a second run. The daemon queues no `Review #<id>` for a fix head that an
open re-review of its lineage covers or that a review already approved.
