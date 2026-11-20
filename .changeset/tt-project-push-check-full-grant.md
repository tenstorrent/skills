---
"tt-project": patch
---

Push checks (the push queue's batch suite, its bisect rounds and `ttp push` checks) now always get one full run's test-slot grant. When task checks hold the pool or the project's test budget, the push branch's reserved lane makes up the rest of the grant instead of granting a single slot, which ran full suites with one pytest worker. The lane holds one push check per branch at a time and task checks never use it.
