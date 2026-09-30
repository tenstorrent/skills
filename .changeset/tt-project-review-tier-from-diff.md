---
"tt-project": patch
---

`tt-project`: review tasks run at the tier their diff needs. A doc-only diff, or one of at most
`review.light_max_lines` changed lines (default 60) that touches no `review.risky_paths` glob, runs light;
anything else runs standard. Deep stays the coordinator's explicit choice, and a retried review never drops to light.
