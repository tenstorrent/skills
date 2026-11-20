---
"tt-project": patch
---

The daemon queues the review of a code task with no branch on record (its project root is not a git repository) from its hand-off's top-level `branch` + `head` (a 7-40 digit sha, optional `repo`), recorded as a `published` change, instead of raising review_unqueued; the worker's hook warns a done or needs_review hand-off that names no PR, `published` change or top-level branch + head, since a branch or sha only in the summary is never read.
