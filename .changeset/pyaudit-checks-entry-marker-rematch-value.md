---
"tt-project": patch
---

A run's inline Python can no longer write the check-slot upload locks or spill-down marks by importing the ttp package: the judge allows them only in the harness's own `python -c` checks entry. A re-matched older rule record now takes the current matcher's item, so a stale stored value is not kept.
