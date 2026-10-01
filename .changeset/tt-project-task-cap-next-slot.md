---
"tt-project": patch
---

`tt-project`: 0.2.27 names when the 24 h new-task cap next frees a slot, wakes the coordinator
then to retry, marks a turn's replies "not done" when some of its actions were rejected, and no
longer counts review tasks toward the cap.
