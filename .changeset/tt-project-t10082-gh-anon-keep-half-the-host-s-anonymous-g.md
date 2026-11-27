---
"tt-project": patch
---

`tt-project`:

- gh-anon: a task run spends at most its share of the host's anonymous budget; pr-watch reads heads by ls-remote once it is spent
- pr-watch: a head read only moves the approval's head without a pr_changed event, within the run's deadline
