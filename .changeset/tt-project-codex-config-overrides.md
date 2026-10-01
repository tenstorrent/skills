---
"tt-project": patch
---

`tt-project`: Codex runs get their system text as `-c developer_instructions` (a TOML string,
added to Codex's own instructions) and their compact window as
`-c model_auto_compact_token_limit`; text over 120 kB still leads the prompt. Resume arguments now
go last. The light compact window default is 100k, and the Claude adapter clamps the window to
Claude Code's 100k-1M range. The Codex overrides follow the Codex config reference and are tested
against fixtures only, not a live Codex build: a build that ignores these keys runs without them.
