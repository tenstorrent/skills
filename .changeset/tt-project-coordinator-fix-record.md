---
"tt-project": patch
---

`tt-project`: each hands-on change a full-tools session coordinator makes is recorded with what, why, the order or task it serves and how to undo it. Before a fix it runs `ttp audit why "<why>" --serves <o12|msg#123|#456> --undo "<how>"`; each later mutating call in that wake carries those fields in `tool-audit.jsonl` and `progress.md`. A mutating call with no why is flagged once in the next coordinator digest, the daily review lists the day's fixes grouped by the order they serve, and `ttp audit fixes [--since <time>]` prints them.
