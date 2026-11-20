---
"tt-project": patch
---

A finished code task whose review could not be queued (a slow git read, the review cap full) is marked review-pending and retried on the next tick; the finish's review read gets its own short git cap. The unreviewed sweep reads every branch head in one batched git call, resumes a cut pass on the next tick until it has looked at every task, and keeps running through a degrade while it has work left.
