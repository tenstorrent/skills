---
"tt-project": patch
---

`tt-project`: a harness task that hands off `"commit": "none: <why>"` because nothing needed changing now gets `TTP_HARNESS_LANDING=none` (a valid no-change landing) instead of `refused`, as long as its branch holds no commit the harness HEAD lacks; a `none:` hand-off with unmerged commits on its branch stays refused so that work is not lost.
