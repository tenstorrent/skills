---
"tt-project": patch
---

`tt-project`: the daemon ends the tools of a run whose supervisor and agent both died even when it finds one of them mid-exec (its environment reads empty for a moment); it looked once and could leave them running for good.
