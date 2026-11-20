---
"tt-project": patch
---

`tt-project`: a template upgrade can no longer loop without finishing. The dispatch duplicate check never holds a template-finish task as a repeat of a done one (each is queued for a merge still left to finish), and `ttp upgrade --apply <commit> --take-over-from <task>` fences that blocked or overdue holder and applies the merge instead of exiting 75.
