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
claim: it is another repository's commit, no check here can read it, and it stays as written.
Destination words match only as whole words, a slash token counts only as a ref a movement word
names (never a ratio, 'and/or' or a file path), and a claim's sentence ends at its nearest stop,
so the next sentence never changes its destination. A hand-off waiting on a push still on its way
(a detached push queued or running, an unsettled pushed list) is the remote check's to settle: its
text is left alone until the push lands or is shown missing, never corrected locally about a push
that has not happened yet.

The sha a sentence names is read by the one sha-phrase parse, so a foreign sha is matched in any case,
a negated phrase ('never pushed as <sha>') names none, and an all-hex word with no digit is a word.
Only a done hand-off the remote check will settle skips the local correction while its push is in
flight; a blocked, failed or approval-needing one is corrected locally, and its claims are read once.

Known gaps: a sha-less push claim naming another destination is not checked; a sentence that also
names a foreign sha stays as written; claims about another task or run, hedged phrasing and unusual
wording pass unchecked; quoted text written to a script and run later is not checked.
