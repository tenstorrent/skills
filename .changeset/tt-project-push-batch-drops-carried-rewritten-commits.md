---
"tt-project": patch
---

Push batches leave out a commit an approved change carries from another review when that review landed it reworked (a new subject or patch) and the push branch already holds every line the commit adds. The copy is recognised by its author, author date and subject, matched through that review's landed approved heads or a Ttp-Replayed-From trailer naming the original. Replaying it could only conflict with its own landing, so the approval holds without a review rerun. A copy that only partly landed, or one on the entry's own branch, is still replayed and conflicts as before.
