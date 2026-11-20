---
"tt-project": patch
---

A coordinator session wake on a fresh project, with no measured resumed wake yet, gets a $3 per-wake cap instead of the $1 turn budget, which sat below a measured session wake. A wake stopped by its dollar cap reruns in the same session with twice that cap, within the project and account dollar caps, so it never stops again at the same cap. A new session's first wake that was kept after its cap or timeout records its seed's size, so its rerun no longer compacts in place right after the seed.
