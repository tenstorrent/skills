---
"tt-project": patch
---

The push queue keeps a review approval across a shared-doc conflict only where both sides just added
lines (two changes appending to one section). It keeps both sides' lines in landing order, reruns the
push checks and records the merge on the task (`doc_merges`: file, hunks, both source commits) and as
a `doc_merged` event. A doc hunk that edits or removes lines now goes back to review, like a conflict
in code, tests or manifests.
