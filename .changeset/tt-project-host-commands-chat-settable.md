---
"tt-project": patch
---

`tt-project`: the coordinator can now set `runner.host_commands` (the commands `ttp host-run` runs on the host outside the sandbox) through chat's `config_set`, and `ttp config --chat-settable runner.host_commands` accepts it. Before, every chat path refused the key although the docs said the coordinator sets it. The value is checked with `ttp host-run`'s own entry checks before anything is stored: it must be a JSON list of command strings or `{"cmd": ..., "timeout_s": N}` entries; a plain shell string, a non-list, or an entry that is empty, not a string, has a bad `timeout_s` or an unknown key, or does not split is refused, naming the entry. `ttp config` applies the same check, so it no longer stores a non-list value.
