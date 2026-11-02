# Providers a project may use

`providers_allowed` in `project.json`: the providers this project's tasks may run on. Unset or empty (the default): every configured provider, as before. Set: the core provider (always allowed: coordinator turns run on it) and the names listed, nothing else.
Set it with `ttp config <name> providers_allowed claude` (several: `claude,codex` or a JSON list); `ttp config <name> providers_allowed '""'` unsets it. Names must be configured providers.

What it keeps inside the list:

- Tier routing: `providers.chat.tiers.<tier>.route` routes nothing while the list leaves chat out; the tier's runs start on the core provider.
- Provider failover and plan-window failover: no task moves to a provider outside the list, whatever `coordinator.provider_failover_to` names. A queued task whose only allowed providers cannot start new work waits on them; its status line starts `held by providers_allowed:` and clears once it starts.
- Task pins: `task_add` and `task_update` refuse a `provider` outside the list with an error naming it. A task pinned before the list was set waits queued, attempts untouched, with a `held by providers_allowed:` line, until its pin is cleared or the list names its provider.

`ttp doctor` (config problems) names a routed chat tier or a failover target the list switches off, a name that is no provider, and a list that leaves out the core provider (still allowed).
