---
"tt-project": patch
---

`tt-project`: a chat-provider (mini-swe-agent) run's runwatch entry is filled from its own step record (its trajectory): its steps become tool calls, so the model-free drift check no longer flags an active run as quiet, loop and repeat_fail work on it as on a Claude run, its spend so far is priced from its own usage counts, and with no hook its steer read state is unknown until the run's own step hands the update over.
