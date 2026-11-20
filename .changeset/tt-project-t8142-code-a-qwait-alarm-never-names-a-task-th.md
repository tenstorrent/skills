---
"tt-project": patch
---

`tt-project`: a checks queue wait is raised only for an open task with a live slot wait while the host queue has someone waiting.
- a push batch's run lock is its holder's own, never a detached push waiting on its own lock
