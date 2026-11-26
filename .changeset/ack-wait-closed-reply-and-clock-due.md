---
"tt-project": patch
---

ack-wait: a reply that closes or declines the request without a run now ends a `--terminal` wait; `settle` records it as needs_review marked `reply_closed`, never as done. Obligations: a clock-time due that passed under an hour ago reads as now, as a start_after does, instead of being refused for lacking a time zone offset.
