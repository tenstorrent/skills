---
"tt-project": patch
---

`tt-project`: a chat route held because its model is missing now names where that model came from: the `ttp status` line and the route-model-missing notice say `providers.chat.routes[<i>].model` for a rule's model and keep `providers.chat.tiers.<tier>.model` for the tier's own. Holds are kept per tier and model, so two missing rule models on one tier hold together with one notice each instead of replacing each other; holds recorded per tier before are still read.
