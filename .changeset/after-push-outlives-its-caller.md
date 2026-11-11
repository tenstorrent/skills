---
"tt-project": patch
---

`tt-project`: the `delivery.after_push` deploy of an ordinary `ttp push` now runs in a process of its own session, handed the receipt's lock, with its output in a log next to the receipt; `ttp push` waits for it and copies the log, but a caller killed meanwhile (for example a `timeout` around `ttp push`) no longer takes the deploy with it. A receipt whose process was killed gets one automatic rerun at the same commit; if a newer deploy of the same branch has started since, it is recorded as superseded (an event, no alert); only a failed rerun alerts. The worker prompt says never to wrap `ttp push` in `timeout`.
