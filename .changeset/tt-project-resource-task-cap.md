---
"tt-project": patch
---

`tt-project`:

- resources: config `resources_tasks.<name>` sets how many tasks naming a resource run at once, apart from its `ttp lock` slots (`resources.<name>`); unset, it stays twice the slots. A task's `resources_read` (task_add and task_update) marks a resource it only reads: it counts for pauses, user orders, per-machine failure attribution and `ttp machines activity`, never for the dispatch cap, and its worker prompt says to run commands there under `ttp lock --shared` and never run a device job or change a service; tests.
