---
"tt-project": patch
---

`tt-project`: tests only: the test of a SIGKILLed `ttp lock` whose command keeps the lock waits for that orphaned command to end on a pidfd instead of a 30 s poll, which a loaded host outlasted while the command was still on its way out.
