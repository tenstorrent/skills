---
"tt-project": patch
---

GLM work fallback: the next runs of a task whose GLM run was refused its model mid-run now take `route.glm_work.fallback` (default Sonnet high) instead of the core tier model; a task that answers a chat keeps its tier's model. Documented with an example.
