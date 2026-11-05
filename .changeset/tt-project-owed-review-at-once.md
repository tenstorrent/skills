---
"tt-project": patch
---

A finished change always gets its review: a question, plan or work task whose worker committed its change on its own `ttp/t<id>-...` branch becomes a code task on that branch before its finish is recorded, so the daemon queues its review. An urgent task, or now also one that owns an open obligation, gets that review in the same pass on a `needs_review` hand-off, at its own priority, instead of sitting in review until a stall alert.
