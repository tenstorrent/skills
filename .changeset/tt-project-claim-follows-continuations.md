---
"tt-project": patch
---

A claim backed by a task that failed, was cancelled or is gone now follows the tasks that continue it (`continues:` and `follows:` labels, `continue:` follow-ups of its parent, hop by hop, each once and at most 50). A continuation that ended done makes the claim hold, with a receipt naming it; an open one keeps the claim waiting until its due time instead of reporting it as not holding. With no continuation the check is unchanged.
