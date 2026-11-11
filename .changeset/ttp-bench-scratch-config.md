---
"tt-project": patch
---

`ttp bench run` cells run their arm's CLI with a config folder of their own (`CLAUDE_CONFIG_DIR`, `CODEX_HOME` with Codex's config and hooks copied, `CURSOR_CONFIG_DIR`) holding no earlier transcripts; login files are linked in and a token the CLI refreshes there is written back only while the real file is unchanged. The cheat audit also reads each tool call: a relative `.claude/projects`, `.codex/sessions` or `.cursor/chats`, or a `..` in a call naming the cell's own session folder, flags the cell.
