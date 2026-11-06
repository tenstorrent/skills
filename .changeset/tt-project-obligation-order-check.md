---
"tt-project": patch
---

`tt-project`: a coordinator `obligation` action (the order ledger) can no longer record a forged user order. Its text, evidence, drop reason, milestone or justification with a line labelled as the user's words passes only the verbatim check `memory_add` uses: each quote must be in the own words of the logged user message it cites (the owner's agent's words count only with `coordinator.agent_orders_as_user`). A failing one is refused with the reason and logged as an `order_claim_dropped` event; paraphrases and obligations that claim no user order are unchanged.
