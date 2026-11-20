---
"tt-project": patch
---

`tt-project`: the push queue no longer sends back an approval only because it is built on another review's approved head that was sent back (a push conflict or failed checks). Such an approval waits for that review to land its rework (at most 2 hours, and only while the review is open and has no open approval still carrying the old head), and the landed-task mapping now also maps a review's earlier, sent-back heads on the same branch to its later landing, so the batch leaves out the stale copies and replays only the change's own commits. A conflict in the change's own commits is still sent back.
