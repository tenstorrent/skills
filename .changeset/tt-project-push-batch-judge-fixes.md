---
"tt-project": patch
---

Push batch judging: before blaming entry 1, only the tests new on its failure are checked against the tip, so a test that already fails on the tip no longer forces a full check of the tip alone. With `checks.judge` false, a test that passes on its rerun is no longer quarantined.
