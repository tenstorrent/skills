---
"tt-project": patch
---

`tt-project`: process reaping now proves a process belongs to this project before signalling it. The stale-lock reaper and the stop of a cancelled task's checks used to choose processes by task or run id and the project folder's name, which are the same across projects on one host. Now a process counts as this project's only when its `TTP_PROJECT` resolves to this project's folder or its `TTP_RUN_DIR` lies inside it; lock labels record the full project path, and `ttp detach` jobs always carry `TTP_PROJECT`. The guide and the harness skill state the rule for project reaper and cleanup scripts.
