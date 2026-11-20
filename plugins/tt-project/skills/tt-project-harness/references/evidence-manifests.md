# Evidence manifests

A hash-pinned manifest lists only immutable artifacts: files that never change after the work.
Operational queues, outboxes, acknowledgement queues and growing logs are mutable: they change or drain after the work, so a manifest never lists one as a required member by its hash.
Pin instead the command and its stdout receipts that show what happened, or an explicitly labelled byte-for-byte snapshot with its provenance (when it was taken and from what).
A review re-hashing a manifest whose member changed or went missing because it was mutable flags the manifest as the defect, not the work.
Queued locally, recorded by the recipient system, and delivered or acknowledged by it are three distinct facts; never state one as another, and evidence of `queued` alone is never evidence of delivery.
This is evidence-authoring only: no transport retry, no new capability owner, no reconstructed bytes and no new acknowledgement action to make a past fact true.
