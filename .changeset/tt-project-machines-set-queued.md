---
"tt-project": patch
---

`ttp machines set` and `ttp machines add` run from a sandboxed worker, whose tt-project home is read-only, queue the checked change in the project's state for its daemon, which saves it once on its next tick, instead of failing with a read-only file system traceback. A write failure with nothing to queue in exits 1 with one line.
