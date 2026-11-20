---
"tt-project": patch
---

`tt-project`: config provenance. Every setting change is logged with its setter, old and new value and undo. A worker, upgrade or migration can no longer overwrite a value the user or a coordinator set (it may still turn an owner's switch its safe way); a refused worker is told to propose the change in its hand-off. An event is raised when a change breaks the checks. `ttp config KEY ''` and the web app's default port save end cleanly on a refused value.
