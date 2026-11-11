---
"tt-project": patch
---

`tt-project`:

- detached checks of a failed, cancelled, exhausted or abandoned task leave the host-wide checks queue instead of holding a place others wait behind: the waiting process checks its own task before it takes a slot, and the daemon asks it to leave on each queue pass. It exits cleanly and records why, so `ttp checks --result` says `dropped: task #N ended (<status>)`, never a bare exit code 143 from a signal mid-wait. Checks an open task adopts stay: one that continues the ended task (or a continue of it) on a branch that still holds their head, or whose hand-off waits on them. Running checks are never stopped by this rule.
