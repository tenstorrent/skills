---
"tt-project": patch
---

`tt-project`: docs say when a change of `checks.wrap_lane` takes effect: it reaches `ttp checks` runs that start after it; a run already queued for the wrapper lane or holding it keeps the mode it started with until it ends, except a waiting run that re-executes for a runtime upgrade, which uses the new setting (follow-up of the question in note 7f760639dcf68b3d).
