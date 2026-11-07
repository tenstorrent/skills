# Chat-settable config

The coordinator's `config_set` writes only the keys below; any other key is refused with a reason.
Check a key, and a value, without setting it: `ttp config --chat-settable KEY [VALUE]` (exit 0 accepted, 1 refused, the reason printed).
`ttp config --chat-settable list` prints every accepted key.
Check each key this way before telling the user a setting is being applied: a key the runtime reads may still be refused from chat.

Besides the fixed list (budget, notify, delivery, review, coordinator and a few provider switches), these provider keys are accepted on the user's word:

- `providers.<claude|codex|cursor|chat>.tiers.<light|standard|deep>.model`: a model name, never a URL; `""` keeps the provider's default.
- `providers.<claude|codex|cursor|chat>.tiers.<light|standard|deep>.effort`: `minimal`, `low`, `medium`, `high`, `xhigh`, `max` or `""` (chat: no `minimal`).
- `providers.chat.tiers.<light|standard|deep>.route`: true or false; true only once that tier's `model` and `providers.chat.endpoint` are set (set route last).
- `providers.chat.tiers.<light|standard|deep>.route_timeout_s`: seconds, more than 0 and at most 14400.
- `providers.chat.endpoint`: the alias of an endpoint already saved on this host with `ttp secret endpoint <alias> --url <base url>`; refused when none is saved, or for a URL.
- `providers.chat.effort_fields`: model -> effort (or `*`) -> plain request fields, each a number, true/false or a short word, such as `{"<model>": {"low": {"reasoning_effort": "low"}}}`; no objects, lists, URLs, conversation fields, or field names naming a URL, host, key, token or header.
- `providers.chat.routes`: an ordered list (at most 32) of rules routing worker runs to chat by kind, tier and label, each `{"model": "<model>"}` with optional `kinds`, `tiers`, `labels_any`, `labels_none` (lists of words), `effort` and `route_timeout_s`; the first match wins, the per-tier `route` is the fallback, `[]` turns rules off. Reviews, coordinator turns, pins, tier-up retries, conclusion and device runs, and urgent, needs_deep, user_deep, review_fix and untrusted_input tasks never route by rule.

The fixed list also holds `runner.host_commands`, the commands `ttp host-run` runs on the host outside the sandbox: a JSON list whose every entry is a command string (the words a request must equal or start with) or `{"cmd": "<command>", "timeout_s": N}` (1 to 3600).
A plain shell string, a non-list, or any entry `ttp host-run` would refuse (empty, not a string, a bad `timeout_s`, an unknown key, a quote that does not close) is refused, naming the entry, and nothing is stored.

Only off from chat: `providers.<p>.write_fence` and `sandbox.checks`, which turn namespace sandboxing on, are user-only.
`config_set` (in a turn carrying a user message too) and `ttp config` inside any run refuse true and accept false or `''`; only the user's own word (`ttp config` in their own shell, or the web settings page) turns one on.
`restrictions.no_internet` also turns sandboxing on (Codex, Cursor) and is a Restriction: `config_set` cannot set it and `ttp config` inside a run refuses it on or off.

Never from chat: any `providers.<p>.base_url`.
Worker runs carry internal code, so the bridge sends them only to the base URL saved with the endpoint's key; leave `base_url` unset.
The key itself never goes in config: only the alias does.
