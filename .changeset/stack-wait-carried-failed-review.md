---
"tt-project": patch
---

A code task that waits on another task's landing while its own branch already holds that task's head is taken as done, even when that task's latest review failed: those commits reach the push branch only through this branch's review, so the wait could never end. The daemon also re-checks a task already asleep on such a wait and finishes its hand-off model-free once its branch holds the other task's head. Such a hand-off is not then sent back to wait on that same landing. A wait on commits its branch does not hold, or on a task whose failed review has an open fix (that fix's landing lands it), is unchanged.
