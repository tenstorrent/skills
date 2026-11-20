---
"tt-project": patch
---

An order or obligation no longer closes just because its owner tasks finished when an owner's hand-off leaves the acceptance unmet (a `partial:` or `cells unreported:` summary, a blocked, waiting or failed hand-off). A reply that delivers it and a reply after its owners end both leave it open, and the obligation list shows it as `held open` with the reason. A receipt whose evidence names only such owners, or that such an owner files itself, is refused with the reason. A met continuation of that owner still closes it. A separately attached successor that finishes met does not carry the incomplete owner's work: the order stays open with its message and due until an explicit receipt. Regression tests also cover an acknowledgement-only run (no hand-off) and an on_pass_cmd that writes a review hand-off and then exits nonzero: neither counts as success.
