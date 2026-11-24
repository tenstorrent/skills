---
"tt-project": patch
---

`tt-project`: a GLM code run's mini-swe-agent task message is now tt-project's own: it asks for independent reads and searches batched into one command or one response, large reads and one-go edits, and no extra reproduce script or edge-case pass unless the task asks, in place of mini's step-by-step default. On mini's message GLM code runs made a median 94.5 tool calls against Opus's 49 at about the same seconds per call. `route.code_sonnet.glm.batch_steps: false` keeps mini's message.
