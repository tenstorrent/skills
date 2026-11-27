---
"tt-project": patch
---

`tt-project`:

- Ponytail lane: only block-severity findings (block, blocker, critical, high, or none given) fail a review; info, minor, low and other severities are notes that never fail it or queue fix tasks
- PR sweep: required checks from the base branch's protection and rulesets count as not run when missing on the head; skipped jobs are judged per run; a dispatch is covered only by the run it produced with the same inputs, or a push or PR run on the head
