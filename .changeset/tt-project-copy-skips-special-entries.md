---
"tt-project": patch
---

`tt-project`: `ttp bootstrap`, `setup` and `upgrade` no longer fail (or hang on a FIFO) when the runtime folder holds sandbox placeholders: tree copies skip `.claude` folders and devices, FIFOs and sockets, and say once on stderr what they skipped. Files and symlinks copy as before.
