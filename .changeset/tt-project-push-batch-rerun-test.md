---
"tt-project": patch
---

`tt-project`:

- the push-batch test for rerunning each failing test not yet judged at a head gives the batch a second check, so prefix 1 is still checked in full (only on the check that never ran on the failing head) since a push train skips the second full run of a check once its search knows the failing tests (91eea3917). Tests only, no behaviour change.
