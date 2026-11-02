---
"tt-project": patch
---

`tt-project`: the Codex system prompt check's observation now says a run without a task (the coordinator's) was only reported, not stopped and blocked. A task run stopped for lacking its system prompt now counts its spend as waste (the runaway guard sees it) and never counts as handed off. A rollout that shows no model reply within 120 s gets the verdict "unchecked", is reported once and is not parsed again for the rest of the run. A run that ends before the check thread is ready still gets its end-of-run check.
