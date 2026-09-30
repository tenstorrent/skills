---
"tt-project": patch
---

`tt-project`: `task_add` checks `depends_on` like `task_update` does. Unknown ids and dependencies
on cancelled or failed tasks are rejected, nothing is created, and the reason reaches the
coordinator's next turn.
