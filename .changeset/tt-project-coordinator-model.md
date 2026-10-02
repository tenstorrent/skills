---
"tt-project": patch
---

`tt-project`: the coordinator can keep its own model and effort. Set `coordinator.model` or
`coordinator.effort` in project.json (or ask the coordinator to, with `config_set`) to pin the
coordinator alone, so moving the light tier to a cheaper model for workers does not move the
coordinator with it. Both are empty by default, which keeps the coordinator on its tier.
