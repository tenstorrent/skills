---
"tt-project": patch
---

`tt-project`:

- Pace drift check: nothing-committed compares this run's spend with this run's commits, not the task's total spend
- Pace drift check: judge this run's spend against the budget it started with and its new commits only
- Pace drift check: nothing new means the branch tip has not moved since the run was first seen
- Pace drift check: test that check() remembers the first-seen branch tip
