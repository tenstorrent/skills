---
"tt-project": patch
---

A task waiting on its own detached `ttp checks` is settled without a model run where the outcome is clear. A waiting hand-off may carry `on_pass`, the final hand-off (`done` or `needs_review`) to record if the checks pass: on a pass on the head still checked out (clean, and a code task's branch still there), the daemon records it through the normal finish, so the review and push queue start as usual, and logs it as `model-free`. Checks that failed or were killed wake the task at once at its own tier, with the end of checks.out in its prompt. Anything unclear (no exit code, a moved head, an `on_pass` that is not a final hand-off) wakes it at light as before. The code and review prompts and `ttp checks --detach` say when to give `on_pass`.
