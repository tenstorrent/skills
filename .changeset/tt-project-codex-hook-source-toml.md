---
"tt-project": patch
---

`tt-project`: Codex workers keep the harness hook (hand-off guard, credential deny, update delivery) when a `config.toml` only mentions hooks. The hook-source check now parses `config.toml` with `tomllib` (or `tomli` where installed) and counts only a top-level `hooks` table or key, or a plugin entry not set `enabled = false`. Commented-out lines, `hooks` inside strings, `[features] hooks = true` and other tables no longer count. Without a parser, or on a file that does not parse, the old cautious pattern still decides.
