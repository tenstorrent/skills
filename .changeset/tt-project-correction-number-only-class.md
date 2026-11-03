---
"tt-project": patch
---

Corrections: when a correction's class is only task, PR or message numbers (`#<n>`, `msg#<n>`), a task
is its class fix only when it cites the correction's message or shares its subject words. A task that
cites the same numbers for another reason, such as watching those PRs, no longer counts as the fix.
