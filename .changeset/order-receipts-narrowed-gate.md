---
"tt-project": patch
---

A change order's receipt is gated on its change landing only where a check is possible: an owner that is a code task with commits of its own, in a project with a push branch. Work, harness and question owners, a done code task without commits, ownerless orders and the coordinator's own receipt close unchecked as before, and no commit id is parsed out of the receipt text, so a hex token that is not a commit can no longer mark an order unverified or hold it open. A receipt that is not landed lands as progress with the order kept open; a check that cannot run keeps it open marked unverified, and a later decided check clears that stale mark.
