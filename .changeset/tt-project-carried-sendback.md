---
"tt-project": patch
---

A code task whose head carries another task's commits, when that task's latest review failed and
those commits are not on the push branch, is sent back once at hand-off. The send-back tells the
worker to rebase onto the push branch's tip and keep only the commits its change needs. If the
change needs that task's code, the worker instead hands off waiting on `ttp landed --task <id>`
and says why. Without this, a review passed the head and the push queue then refused it. No
send-back happens when the task already waits on that landing, when the carried commits have
landed, or when the carried change is approved for the push queue.
