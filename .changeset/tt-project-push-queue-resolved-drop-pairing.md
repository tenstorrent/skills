---
"tt-project": patch
---

`tt-project`: the push queue drops an amended entry commit as landed through a conflict resolution only when each line it adds that the landed copy lacks pairs, one to one, with a similar line the resolution rewrote (one line holds every word of the other, or they read at least 60% alike). A line the resolution added of its own no longer hides an amend's line: an unpaired line keeps the commit in the replay, so its work lands or comes back as a conflict. Each such drop is now also a queued `push_dropped_as_resolved` event for the coordinator (once per batch and commit) and a line in `ttp push --result`.
