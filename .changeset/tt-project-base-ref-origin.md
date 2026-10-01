---
"tt-project": patch
---

`tt-project`: Code tasks now branch from `origin/<push branch>` (or the charter's branch) when the
remote has it, so a lagging local copy is never used as the base. The docs-only push check lists
moved files under both names, so code moved into `docs/` still counts as code.
