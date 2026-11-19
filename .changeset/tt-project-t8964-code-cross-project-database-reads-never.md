---
"tt-project": patch
---

`tt-project`:

- globalcap.connect_ro: reading another project never makes or changes its -wal/-shm; a stopped project's db is read immutable, or from a private snapshot when its -wal holds commits; every cross-project reader goes through it
- smoke list: a foreignread case
- durable-write exemption: the read snapshot is a cache
