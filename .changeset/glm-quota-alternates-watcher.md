---
"tt-project": patch
---

`tt-project`: the chat watcher (the coordinator's wake gate and pre-screen calls) now uses the per-model quota buckets while the release flag `glm_quota_alt` is on (off by default). A model whose bucket is empty is skipped without a call. When every model it would ask is empty, the call goes to the first model in `providers.chat.quota_alternates` that is served, not down and has room. A `flash` model is used only once it has been measured. With no alternate that has room, the chat endpoint holds until the earliest bucket reset rather than cooling down and retrying into more 429s, and the core provider decides meanwhile, as the last fallback. A quota 429 or a rate-limit header from the watcher's own calls updates the bucket. Person-facing runs never move. With the flag off, nothing changes.
