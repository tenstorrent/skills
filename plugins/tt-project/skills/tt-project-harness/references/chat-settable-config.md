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

Never from chat: any `providers.<p>.base_url`.
Worker runs carry internal code, so the bridge sends them only to the base URL saved with the endpoint's key; leave `base_url` unset.
The key itself never goes in config: only the alias does.
