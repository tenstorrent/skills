---
"tt-project": patch
---

The unchanged-head rule never closes a push-conflict rebase task before it runs, and a review the push queue failed only for a push conflict (its approval stood) no longer counts as a failed verdict for that rule or for task_add's refusal. When the rule does close a review, its one event names any approved push of that head left unlanded.
