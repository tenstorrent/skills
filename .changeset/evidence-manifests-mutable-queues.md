---
"tt-project": patch
---

`tt-project`: kind-review.md's review guidance and coordinator.md's review-hand-off line now say that evidence manifests pin only immutable artifacts: a mutable queue, outbox, acknowledgement queue or growing log is never a required member by hash; pin the command and its stdout receipts or a labelled byte-for-byte snapshot instead. They also distinguish three facts — queued locally, recorded by the recipient, delivered or acknowledged — so a manifest never claims one as another, and evidence guidance stays evidence-authoring only (no transport retry, no new capability owner, no reconstructed bytes, no acknowledgement action).
