---
"tt-project": patch
---

A question or work task that committed on its own branch (such as a `continue:` hop that kept its
parent's kind) no longer stays in review after its review passed and the push queue landed its work:
the push queue closes it as it closes a code task, and `ttp task superseded` accepts it. A task with no
branch on record, a review and a harness task keep their current handling.
