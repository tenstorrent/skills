---
"tt-project": patch
---

`tt-project`: evidence manifests pin only immutable artifacts. The tt-project-harness skill gains an "Evidence manifests" row and `references/evidence-manifests.md`: operational queues, outboxes, acknowledgement queues and growing logs are mutable and never required hash-pinned members; pin the command and its stdout receipts or an explicitly labelled byte-for-byte snapshot with its provenance instead. Queued locally, recorded by the recipient, and delivered or acknowledged stay three distinct facts, and the guidance is evidence-authoring only (no transport retry, no new capability owner, no reconstructed bytes, no acknowledgement action). worker.md tells manifest authors the same in one line; kind-review.md tells reviewers that a mutable member which changed or vanished is a manifest defect, not a work failure, and that queued is not delivered; coordinator.md's review hand-off line says a manifest never pins a mutable queue by hash.
