---
"tt-project": patch
---

`tt-project`: memory index writes (adding or retiring an entry, undoing one after a rollback) and the daemon's merge of a harness task branch now share a short file lock in `state/`. Before, an append to `MEMORY.md` that landed while a merge was rewriting the file could be lost. A writer waits at most 20 s for the lock and then writes anyway, so no entry is ever dropped; the merge waits at most 5 s and otherwise tries again on the next pass.
