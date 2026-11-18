---
"tt-project": patch
---

`tt-project`:

- de-slop step 1: ttp slop scan, review spec carries scan delta and rule, worker and daily-review prompts, docs, tests
- de-slop: shorter code-worker line, cap ledger, slop cache exempt from durable-write check
- de-slop step 2: deslop schedule (flag deslop.enabled), one refactor task per pass, hold and pause skips, docs, tests
- de-slop: slop git reads go through gitmemo.run; pause test sets the paused flag
- de-slop: shorter code-worker line fits the worker prompt cap after the rebase
- de-slop: a deslop schedule that never ran is due once at enable, not a period away
- rollout health: a forward revert counts only against a flag whose gated code the reverted commit touched, never a shadow flag; unresolved reverts are listed unattributed
