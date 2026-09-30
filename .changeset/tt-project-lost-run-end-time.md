---
"tt-project": patch
---

`tt-project`: a run found lost after downtime ends at its last lease or output write, not at the
moment it was reaped, so its cost is not spread over the downtime.
