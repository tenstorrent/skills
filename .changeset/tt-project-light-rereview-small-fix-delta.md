---
"tt-project": patch
---

`tt-project`: a re-review after a failed review now runs at the light tier when its fix delta (the failed head to the new head) passes the light rule (at most `review.light_max_lines` non-doc lines, no risky path or name) and does not span a rebase or merge of new upstream code. Other re-reviews, retries and re-reviews whose delta cannot be measured stay standard; deep stays deep. The run note records the tier and the rule that picked it.
