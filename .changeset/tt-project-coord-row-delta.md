---
"tt-project": patch
---

Coordinator session mode: a resumed wake sends only the new and changed rows of the Open tasks
section, with a line counting the unchanged ones and naming the tasks no longer open. A row whose
only change is its age or read time is not resent. Every whole-digest case is unchanged. On the
recorded 120-wake fixture: 5721 prompt tokens per wake full, 4895 by section, 3849 by row.
