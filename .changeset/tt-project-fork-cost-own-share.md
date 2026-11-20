---
"tt-project": patch
---

A worker or review run that starts as a fork of the coordinator's session books only its own tokens
(at list price), not the coordinator session's cost that Claude Code's total includes. A later resume
of that session nets against the highest total an earlier run reported. On its first start after the
upgrade, each project re-books its forked runs once and takes the difference off their tasks' spend,
so the dollar caps, task budgets and top-ups stop counting the phantom.

The correction is also booked in the spend ledger at each run's own time, so the global daily total drops by it.
