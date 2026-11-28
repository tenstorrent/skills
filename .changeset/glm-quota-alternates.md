---
"tt-project": patch
---

`tt-project`: per-model quota buckets on the chat endpoint, behind the new release flag `glm_quota_alt` (off by default). A quota 429 or a rate-limit header with nothing left marks that model's bucket empty until its reset, account-wide. While the flag is on, no run goes to an empty bucket: a routed work or code run takes the first model of the account setting `providers.chat.quota_alternates` (empty by default) that is served, not down and has room, and the route record says which model and why. With none, the run starts on the core provider as before. Review, plan, question, conclusion and chat-reply runs never take an alternate, and a `flash` model is taken only once under 5% of its calls are refused.
