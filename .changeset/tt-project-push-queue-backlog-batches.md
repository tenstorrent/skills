---
"tt-project": patch
---

`tt-project`: the push queue keeps up with a backlog. Priority 1-2 approvals always go into the next batch and lead it. Approvals that waited while the last batch ran start the next one at once, with no extra wait for running reviews. After a clean batch, a backlog bigger than `delivery.push_batch_max` grows the batch up to three times that size; one merged-result check still covers it, and a failing batch is still bisected. `delivery.push_solo_paths` is now off by default. The batch's prompt-size cap probes already find the change that breaks a cap, so holding prompt changes to one per batch only made them wait several batches. When the key is set, it holds only routine changes, and the oldest held one leads the next batch. The `push_fast_waiting` event is gone: with this pick order it had nothing left to report.
