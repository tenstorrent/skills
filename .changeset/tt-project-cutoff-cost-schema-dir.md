---
"tt-project": patch
---

`tt-project`: a run that gave up waiting for an exclusive resource before its agent started is
no longer charged a cut-off cost, and Codex output schemas are written to a per-user cache
directory instead of the shared temp directory.
