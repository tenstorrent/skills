---
"tt-project": patch
---

`tt-project`: the restriction audit now finds tool lines that name a machine by a hyphenated alias (such as `box-a` or `tt-box-3`) from a changed or retired Restrictions item. Before, only words of four or more letters counted, so the alias's short parts were dropped and a line like "hold device jobs on box-a" matched only if two other rare words happened to appear with it. Now letters and digits joined by '-' (with at least one letter) count as one whole word of the item, and a hyphen also bounds it: `box-a` never matches `box-b`, `box-a2` or `my-box-a`.
