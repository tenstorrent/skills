---
"tt-project": patch
---

`tt-project`: the self-watch now stops a run only on its own directly counted process rate (new pids in its tree seen at a sample or in the 1 s pid burst, each counted once). The host's fork rate is never shared out to a run; host churn the runs' own counts do not explain is reported once as unattributed churn, never acted on.
