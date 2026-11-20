---
"tt-project": patch
---

`tt-project`: an open run of an llm schedule holds its next trigger only for the schedule's own period (6 h for a schedule with no period). A run stuck open past that no longer holds the schedule forever: the next trigger admits a run and the daemon log says so.
