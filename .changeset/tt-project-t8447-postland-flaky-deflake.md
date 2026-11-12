---
"tt-project": patch
---

`tt-project`: flaky tests found by post-landing checks get the day's deflake task of their module, as the tests gate's flakes do, and a model-free daemon sweep files any active quarantine entry that no deflake task has listed since it was first seen.
