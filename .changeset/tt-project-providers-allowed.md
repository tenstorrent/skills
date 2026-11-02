---
"tt-project": patch
---

`tt-project`: a project can name the providers its tasks may run on with `providers_allowed` in `project.json` (`ttp config <name> providers_allowed claude`; unset: every configured provider, as before; the core provider is always allowed). Tier routing to chat, provider failover and plan-window failover never send a task outside the list: a queued task whose only allowed providers are gated waits on them with a `held by providers_allowed:` status line that clears once it starts. `task_add` and `task_update` refuse a provider pin outside the list, and a task pinned before the list was set waits queued, attempts untouched. Config problems name a routed chat tier or failover target the list switches off.
