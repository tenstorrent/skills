---
"tt-project": patch
---

`tt-project`: a new code task that shares a file with an open code task now depends on it, and a task whose only unmet dependency is an approved or batched code change starts early on that head; a spec with a long history estimate gets a split note (`overlap.auto_depend`, `overlap.early_deps`, `overlap.split_hours`).
