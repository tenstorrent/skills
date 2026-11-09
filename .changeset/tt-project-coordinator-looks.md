---
"tt-project": patch
---

The coordinator sets its own next check-in with a `look` action (when, what to check and why, and an
optional model-free check that skips the wake when nothing changed). A pending look replaces the
fixed idle wakes. If the tick loop is stuck, the overseer starts the due turn itself; one start
lock keeps the two paths from starting two turns. A backstop sweep that keeps overrunning its
budget sits out a few ticks, recorded once per episode as one incident with stacks and a low note
that rides along with the next coordinator turn; it never starts a turn itself, and every other
tick step always runs, slow or not.
