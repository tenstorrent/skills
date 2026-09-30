---
"tt-project": patch
---

`tt-project`: `task_add` and `task_update` report each invalid resource name they drop, so a typo
no longer leaves a task silently without its resource.
