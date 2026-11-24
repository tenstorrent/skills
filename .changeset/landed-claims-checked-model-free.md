---
"tt-project": patch
---

A run's landed-claim is checked without a model: a hand-off or note that says its change landed, was
pushed or sits on the push branch is corrected to read 'not landed' unless the commit is on the push
branch (push receipts, the branch's tracking ref), marked unverified when nothing can check it, and
a code task whose only fault is the false word goes to its review instead of failing.
