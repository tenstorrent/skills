---
"tt-project": patch
---

`tt-project`:

- an order's or promise's ETA follows its live owners: an owner that is cancelled, abandoned or superseded (`ttp task abandon`/`superseded`) no longer hides a live owner's measured ETA, and a failed or cancelled owner whose continuation carries the work on stands for that continuation (`obligations.live_owner_ids`). With no live owner left the ETA reads `no live owner task (all cancelled or replaced)`. The long-promise check's urgency reads the same live owners. The stored owner lists, the ledger and receipts keep every owner; tests
