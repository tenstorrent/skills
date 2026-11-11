---
"tt-project": patch
---

`tt-project`: the hand-off claim check compares a row's `model` with the run's model only when it names an LLM or provider model (a family from the price table, the providers' tier defaults, the known model names, or a model this project's runs recorded), so a deliverable's own model, such as a render's video model, is never flagged. A row's `llm_model` is always compared. Hand-offs that already move the deliverable's model to its own key, such as `video_model`, keep passing.
