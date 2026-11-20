---
"tt-project": patch
---

- The public scan test drops the commit's short hash from the refusal before checking that it does not echo the task id, so a hash holding those digits no longer fails it.
- The ponytail size rule's refusal says the reason goes on its own line, starting with `why this size:` and followed by the reason (the phrase mid-line is not read).
- The retired-rule echo scan reads an open task's live spec: a step a task_update replaced or withdrew is no longer flagged through the update's own record of it.
- A step stop raised where Python swallows exceptions (a weakref or garbage-collector callback, a `__del__`) fires again just after it, so the step still ends at its budget; the secret env test waits for each job wrapper to exit before scanning the state.
