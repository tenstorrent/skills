---
"tt-project": patch
---

`tt-project`: a plain spec edit on a waiting task no longer makes the wait gate start a paid turn at its next timer or planned check while the probe still says "not yet". The edit applies at the next real wake (the probe passes, its answer changes, a failure wake); `wake_now` and an added `urgent:` line still wake the task at once.
