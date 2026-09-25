---
"tt-buddy": minor
"tt-skills": minor
---

Add the optional `tt-buddy` plugin: a coding agent with Tenstorrent operating
principles. It takes notes all the time, learns the codebase when needed, and
keeps the diff minimal, in a strict, concise voice.

- `buddy`: dispatch table, tone, and writing rules, loaded by session hooks.
- `run`: device runs through `tt-device-mcp`, with auto-triage on hang.
- `learn`: codebase research into dated notes.
- `note`: git-tracked notes timeline in `~/.tt-buddy/notes/`.
- `skill-creator`: design and audit rules for tt-buddy skills.

Tested on Claude Code and Codex 0.155.1: skill listing, hooks, `note`, `learn`,
and a `run` job through `tt-device-mcp` on a T3K. Codex needs its hooks trusted
once (`/hooks`, then `t`).

The finder catalogue lists `tt-buddy`.
