---
"tt-project": patch
---

A failed (changes_needed) review can be closed without a model call. When a re-review that continues it passes, the review is marked superseded automatically. By hand, `ttp task <project> superseded <id> --by-review <passed re-review id>` or `ttp task <project> superseded <id> <covering commit> [--ref <ref>]` closes it. Its verdict is kept, and the daily review, the blocker sweep and the changes_needed lists stop listing it.
