---
"tt-project": patch
---

`tt-project`: the coordinator's action schema lists `force` (task_add) and `reversible` (ask_user), so a provider that enforces the schema strictly no longer drops them; a test checks that every action key the coordinator reads is in the schema.
