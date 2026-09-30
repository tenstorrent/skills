---
"tt-project": patch
---

`tt-project`: review tasks run at the tier their diff needs. A diff that touches no `review.risky_paths`
glob runs light when it is doc-only or changes at most `review.light_max_lines` non-doc lines (default 60);
anything else runs standard. Deep stays the coordinator's explicit choice, and a retried review never drops to light.
