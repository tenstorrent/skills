---
"tt-project": patch
---

`tt-project`: 0.2.26 applies a coordinator turn's `config_set` actions before its other actions,
so a cap raised in a turn counts for that turn's `task_add` actions.
