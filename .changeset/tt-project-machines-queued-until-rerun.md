---
"tt-project": patch
---

Rerunning `ttp machines set` or `ttp machines add` with a relative `--until` (such as `3d`) from a sandboxed worker no longer queues a second request: the queued request is known by the command's own arguments, so a rerun adds nothing, as its message says.
