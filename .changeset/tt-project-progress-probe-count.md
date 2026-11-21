---
"tt-project": patch
---

`tt-project`: a wait probe whose last output line `progress: <n>/<target>` shows a higher n than before counts as progress, so a steadily filling wait no longer raises a probe stall.
