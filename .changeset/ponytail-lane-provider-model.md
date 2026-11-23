---
"tt-project": patch
---

`tt-project`: the ponytail lane pins its subagent to a model the run's provider can start. A Claude Code run (the claude and chat providers) keeps `review.ponytail_model` (default `opus`); another provider gets that model only where its tiers name it, else its own deep-tier model, or the strongest model its subagent tool offers when that tier lets the provider pick. The review's lane step and the code worker's own lane step both say so, and both say what to do when the subagent tool refuses the named model: start it on the strongest model the tool offers and record `model` in `ponytail.json`. Only with no subagent tool at all does the reviewer run the lane itself.
