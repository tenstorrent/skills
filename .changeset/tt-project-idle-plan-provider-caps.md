---
"tt-project": patch
---

`tt-project`: an idle plan provider no longer uses up the dollar caps. Any provider with a window
reading in the last 7 days is treated as on a plan, so its unbilled cost stays out of the daily
and weekly caps.
