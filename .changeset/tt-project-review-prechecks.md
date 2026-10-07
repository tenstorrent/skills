---
"tt-project": patch
---

`tt-project`: With the push queue on, the daemon starts the project's checks on a code task's head
as it queues the review, and starts the review once they end (at most an hour later). The reviewer
reads their result in its first run and its own `ttp checks` reuses the recorded pass. Reviewers in
queue mode run only focused tests and never wait on the full suite, which the batch runs anyway.
