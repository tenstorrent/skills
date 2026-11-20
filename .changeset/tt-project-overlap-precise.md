---
"tt-project": patch
---

`tt-project`:

- task_add's file-overlap warning lists only tasks a new code task can build on: an open one (advice `depends_on`), or a done one that finished within `overlap.branch_days`, has a branch head and is still to land (advice `start_when landed:#<id>`). Tasks landed through a successor that carried their work (a review, fix or continuation), on the push branch by patch, or shelved, rejected, superseded or cancelled no longer count, so the warning never suggests a landed wait that cannot pass. It names the three most recent matches, then `+N more tasks`; a worker's prompt lists the eight most recent unlanded branches. Still a warning, never a refusal.
