---
"tt-project": patch
---

`tt-project`: `providers.chat.routes` routes worker runs to the chat provider by kind, tier and label, not only by tier: an ordered rule list where the first match wins, at the rule's model, effort and time limit, with the per-tier `route` as the fallback. Reviews, coordinator turns, pins, tier-up retries, conclusion and device runs never route, and with a rule list set urgent, needs_deep, user_deep, review_fix and untrusted_input tasks stay on the core provider. The run's route record names the matched rule. Off by default (an empty list); `config_set` and `ttp config --chat-settable` accept the key.
