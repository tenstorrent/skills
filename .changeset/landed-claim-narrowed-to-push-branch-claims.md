---
"tt-project": patch
---

A sha-less landed-claim is checked against the push branch only when its sentence names that branch,
or says 'landed' naming no other destination; a push or merge to the run's own branch, a PR branch
or a fork stays as written. Known gap: a sha-less push claim naming another destination is not
checked.
