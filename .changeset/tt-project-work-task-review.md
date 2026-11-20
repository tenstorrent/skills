---
"tt-project": patch
---

`tt-project`: a finished work task whose branch changes anything now gets the daemon's review at its hand-off (and from the review sweep), as a code task does, so its commits no longer sit on a local-only branch. One predicate decides the split, the review, the no-new-commits check and the continue hop for both kinds, so a work hop that hands off another `continue:` hop is a split with no review of its half-done head. Question and plan tasks are unchanged: their committed branches still reach the coordinator as local-only notices (a documented gap).
