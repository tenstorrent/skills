---
"tt-project": patch
---

`tt-project`: adding a Restrictions item no longer queues a needless tool-update task. When a charter edit's old and new Restrictions are compared, items are matched by their text (bullet, case, spacing and end punctuation aside): an inserted item counts as added, a reworded one as changed, a dropped one as removed, and the items next to an insertion, or a reorder, count as unchanged. Tool-update tasks are queued only for changed or removed items.
