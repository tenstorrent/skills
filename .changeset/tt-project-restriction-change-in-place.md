---
"tt-project": patch
---

A user's change to a restriction is rewritten in place. The coordinator prompt says to edit the Restrictions item in that same turn (`charter_update` with `quote` or `replaces`), never to leave it standing next to a new section that says otherwise, and to `quote` a permanent item when a temporary section loosens it. If a user turn adds allowing or narrowing text about something an unchanged Restrictions item covers, without editing Restrictions, the update is still applied and a `charter_conflict` event reaches the next digest (at unblock effort), naming the stale item.
