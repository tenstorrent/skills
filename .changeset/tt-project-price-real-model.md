---
"tt-project": patch
---

A Claude run's own tokens are priced at the model its result names (modelUsage, else the session's
init model), never at the alias the run asked for ('opus', 'sonnet', 'haiku'), which no price row
names and so fell back to the dearest row. Claude Haiku 5.5 is in the price table. A model id the
table lacks still books at the dearest row and is told once. On its first start after the upgrade,
each project re-books once the forked runs of 2026-11-07 and 2026-11-08 that were priced from an
alias, taking the difference off their tasks' spend and booking it in the spend ledger at each run's
own time, so the dollar caps and the global daily total drop by it; the daemon log names the runs
and the dollars before and after.
