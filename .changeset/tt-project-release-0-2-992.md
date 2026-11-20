---
"tt-project": patch
---

tt-project 0.2.992: Sonnet (and any non-coordinator-model) worker and reviewer runs share one cached system prompt across tasks while memory changes. The worker system prompt carried the project memory and user corrections, so every memory entry the coordinator added changed it, and the next worker or reviewer wrote its whole tools-and-system prefix again (a first call read nothing and wrote 33-37k tokens). Memory and corrections now lead each run's own prompt instead; the system prompt holds only the restrictions, the shared worker rules and the charter digest, so it stays byte-identical for every worker and reviewer until the charter or rules change, and back-to-back same-model runs read it from the cache (about 20k tokens). Cache lifetime stays at 5 minutes: over the last 54 Sonnet runs a 1-hour cache would have cost more.
