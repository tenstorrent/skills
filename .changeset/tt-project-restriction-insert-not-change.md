---
"tt-project": patch
---

`tt-project`: adding a Restrictions item no longer queues a needless tool-update task. When a charter edit's old and new Restrictions are compared, items are matched by their text (bullet, case, spacing and end punctuation aside): an inserted item counts as added, a reworded one as changed, a dropped one as removed, and the items next to an insertion, or a reorder, count as unchanged. Tool-update tasks are queued only for changed or removed items, or for an explicit `excepts` exception; a new item sent with `both_hold` beside a standing one it overlaps is an addition and queues none. An exception's tool search uses only the item's cites, identifiers and the words the exception names too, never the item's other keywords.
