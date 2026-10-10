---
"tt-project": patch
---

`tt-project`: The push queue refuses an approved head that carries commits of another task whose
latest review failed (matched by hash or patch-id, so a rebased copy counts). The review runs again
told which commits; it approves a head without them, or judges them and lists the task under
`"inherited"` in its push entry.
