---
"tt-project": patch
---

`tt-project`: finding the tasks a review's spec names by branch no longer slows down on long
punctuation runs. The scan now tries only cut points that a known branch could start at and that
are no longer than the longest branch, so one 10,000-character `=====` rule takes milliseconds
instead of seconds on the push queue's approval and settle path. Matches are unchanged.
