---
"tt-project": patch
---

`tt-project`: every Claude worker now gets the same system prompt (rules, charter, memory),
whatever its kind or tier, so all workers share one cache entry. The kind's rules moved into the
user prompt next to the task. Memory entries with the same timestamp are listed in a fixed order.
