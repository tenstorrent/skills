---
"tt-project": patch
---

A model the chat provider refuses (model_missing) stays held when the GLM work route, the code arm or a model
pick chose it, even where its tier is not in providers.chat.tiers. Before, the hold ended on the next tick ("the
tier is no longer routed") and the same task went straight back to the same model, with a new notice each time.
A task refused its routed model now stays on the core provider for an hour, so the endpoint's quiet retry
of a model it still lists runs on another task. The notice goes out once per provider and model within a day,
whatever the tier, and names the setting that chose the model.
