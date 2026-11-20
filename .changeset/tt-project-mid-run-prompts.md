---
"tt-project": patch
---

`tt-project`: the worker prompt asks a worker to acknowledge each mid-run coordinator update with one `ttp note "update: <what changes in my plan>"` line, and says a newer order wins over older spec steps. The coordinator prompts say an update reaches the worker on every provider, that it is acknowledged that way, that one unread after 5 minutes raises `steer_unread`, and (persistent session) to check that the running tasks an order reaches acknowledged it, correcting again or restarting one still on the old plan. The guide documents the acknowledgement and how a user order or charter change reaches running tasks; the provider docs no longer say Codex workers read updates only from `steer.md`.
