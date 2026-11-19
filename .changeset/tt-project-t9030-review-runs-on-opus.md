---
"tt-project": patch
---

`tt-project`: every review run on Claude, its retries and re-reviews included, starts on Opus at high effort or more, even when a re-review is sized light and the project's light tier maps to a smaller model; only a model pin the user required keeps its model. `route.core_cheap` (the routine class included) no longer moves a review, so per-class stats no longer count review runs as cheap runs. A review over `route.review_small_lines` is now of class `hard`, never `routine`; `route.review_medium_lines` is retired (`ttp upgrade` drops it from project.json).
