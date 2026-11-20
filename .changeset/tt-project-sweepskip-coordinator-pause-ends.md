---
"tt-project": patch
---

`tt-project`: the model-free blocker-sweep skip now counts a resource pause the coordinator set when something model-free re-evaluates or ends it before the next sweep: an `end_when` probe whose last answer is fresh and not broken, an enabled heal check's health end, or, with neither, an `until` before the next interval sweep is due. The receipt names each pause with its rule (probe, heal, end before the next sweep). A pause whose reason, `until` or `end_when` changed since the last model sweep, a broken or stale probe, a disabled heal check, a dated-only end past the next sweep, or a pause past its end that was never lifted still starts the model sweep; user pauses and the 24 h model-sweep floor are unchanged.
