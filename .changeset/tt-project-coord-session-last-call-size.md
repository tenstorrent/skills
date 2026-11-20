---
"tt-project": patch
---

`tt-project`: a persistent coordinator session now resumes across wakes unless its real context is near its limit. The session's size is its last model call's prompt and answer, not every call of a turn summed, and the rollover limit is the session's first prompt plus `coordinator.session.rollover_growth_tokens` (100000), capped by a known context window less `rollover_margin_tokens` (40000), so a project whose prompt alone is over 140k tokens can resume. `rollover_tokens` above 0 still sets a fixed limit; its default is now 0.
