---
"tt-project": patch
---

The push queue's batch checks (the check run, its reruns and the cap probe) now run at nice 19 and idle I/O priority, like `ttp checks` and `ttp push` suites, so a host's live services go first. They used to run at the push runner's nice level with normal I/O. A host without `ionice` runs them under `nice` alone, as before.
