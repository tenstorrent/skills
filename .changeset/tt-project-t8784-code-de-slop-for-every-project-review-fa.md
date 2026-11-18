---
"tt-project": patch
---

`tt-project`:

- de-slop step 1: ttp slop scan, review spec carries scan delta and rule, worker and daily-review prompts, docs, tests
- de-slop: shorter code-worker line, cap ledger, slop cache exempt from durable-write check
- de-slop step 2: deslop schedule (flag deslop.enabled), one refactor task per pass, hold and pause skips, docs, tests
- de-slop: slop git reads go through gitmemo.run; pause test sets the paused flag
- de-slop: shorter code-worker line fits the worker prompt cap after the rebase
