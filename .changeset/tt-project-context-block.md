---
"tt-project": patch
---

`tt-project`: each worker prompt gets a "Context from the project's records" section, built without a model call: the user's verbatim words of the orders the task owns (relayer context marked as data), the outcomes, failure reasons, review findings and result.json paths of earlier runs in its lineage, matching tried-work ledger entries, and memory decisions and facts citing the task, its lineage or its orders. What the spec already says is left out, the section is capped at about 3000 characters with paths for the rest, and no linked records means no section. `task_add` notes (never rejects) a spec that quotes none of its order's words or a continue/fix spec that names none of the earlier findings. Hand-offs take an optional `context_gaps` list, counted in the daily review's self-efficiency audit.
