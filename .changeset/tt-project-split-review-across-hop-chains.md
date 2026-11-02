---
"tt-project": patch
---

`tt-project`: when a later `continue:` hop of a multi-hop split chain stalls, the chain's queued review (titled or keyed for any earlier split task of the chain) is released to the latest done split hop's head and reviewed now, instead of waiting on the stalled hop while the unreviewed sweep queues a second review of that head.
