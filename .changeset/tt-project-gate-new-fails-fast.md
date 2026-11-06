---
"tt-project": patch
---

`tt-project`:

- `ttp checks`: a spill host's run whose failing tests the tests gate proved new there (they fail twice on the head and pass on the base on that host) now fails at once instead of rerunning the whole suite on this machine; the blocking verdict is saved per head tree and check, so a later run of the same tree fails before it queues for a checks slot and names the tests to fix (`--fresh` runs it again) (#6509)
