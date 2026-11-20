---
"tt-project": patch
---

tt-project: `delivery.post_land_checks` also run after a task's own `ttp push` to the push branch, not only after push queue batches and express landings. Every landing since the last check is in one run, ordered by position on the branch; an own push is a bisect point of its own (from the landing before it), so a failure it caused is reverted forward like a queued entry's.
