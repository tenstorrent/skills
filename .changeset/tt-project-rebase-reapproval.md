---
"tt-project": patch
---

A rebase review's re-approval lands: the push queue honours a rebase review's
head once no earlier row of that task is still queued or in conflict (one
cancelled or sent back by a moved target no longer blocks it), and accepts a
head whose change has the same `git patch-id --stable` as a reviewed ref (the
reviewed change rebased), refusing any other patch.
