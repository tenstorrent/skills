---
"tt-project": patch
---

A run's landed-claim is checked without a model: a hand-off or note that says its change landed, was
pushed or sits on the push branch is corrected to read 'not landed' unless the commit is on the push
branch (push receipts, the branch's tracking ref), marked unverified when nothing can check it, and
a code task whose only fault is the false word goes to its review instead of failing. A claim the
local tracking ref cannot confirm is checked again after fetching the push branch, so a landing
another route pushed is never called 'not landed' on a lagging ref.

A sha-less claim is checked against the push branch only when its sentence names that branch, or
says 'landed' naming no other destination; a push or merge to the run's own branch, a PR branch or
a fork stays as written. A sentence naming a sha this repository does not hold is no sha-less
claim: it is another repository's commit, no check here can read it, and it stays as written. Destination words match only as whole words, a slash token counts only as
a ref a movement word names (never a ratio, 'and/or' or a file path), and a claim's sentence ends
at its nearest stop, so the next sentence never changes its destination.

Known gaps: a sha-less push claim naming another destination is not checked; unusual phrasings pass
unchecked; quoted text written to a script and run later is not checked.
