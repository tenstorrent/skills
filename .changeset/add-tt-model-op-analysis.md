---
"tt-model-op-analysis": minor
"tt-skills": patch
---

Add the optional `tt-model-op-analysis` plugin. `static-op-analysis` traces every ttnn call of a
tt-metal model test to its device op and program factory for P100, P150 and Quasar profiles at a
pinned commit; `measured-op-analysis` runs the same test under Tracy and reports executed ops,
host versus device op time and max cores per op, with a diff against the static table. A shared
validator checks row ids and launch totals before any output. The finder learns to recommend it
without installing it.

Scripts are covered by unit tests. The measured flow has not yet been validated on hardware.
