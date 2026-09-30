---
"tt-project": patch
---

`tt-project`: a re-review is sized by the fix, not the whole stack. A failed review records the
head it reviewed in `metrics.reviewed_head`. A later review that continues it, directly or through
the fix task it depends on, measures its diff from that head when the branch still descends from
it, with the same `review.light_max_lines` and `review.risky_paths` rules. A missing or rewritten
head, or no change since, falls back to the whole stack. The re-review's spec lists the earlier
findings.
