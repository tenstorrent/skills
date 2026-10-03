---
"tt-project": patch
---

`tt-project`: the once-per-window marker for a budget red alert is saved in the same transaction as
the alert, so a crash between them can no longer silence the window's alert.
