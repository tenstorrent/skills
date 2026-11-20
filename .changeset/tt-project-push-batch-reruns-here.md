---
"tt-project": patch
---

`tt-project`: in a push batch, only a check's first full run may spill to a spill host. Every rerun of its failing tests, by the judge and by the search, now runs on the project host with its test selection, instead of going to the spill host and losing that selection.
