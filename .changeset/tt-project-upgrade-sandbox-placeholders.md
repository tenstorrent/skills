---
"tt-project": patch
---

`tt-project`: `ttp upgrade`'s pre-upgrade commit no longer stages the empty files the worker sandbox leaves at the paths it mounts over (`.bashrc`, `.gitconfig`, `.mcp.json`, `.claude/hooks` and the rest of one list in the Claude provider), which broke every checkout and rebase in a sandboxed task worktree. A change to such a path with real content is left out too and named. An upgrade also untracks, in one commit, the empty placeholders an earlier upgrade committed, keeping the files on disk; one with content is kept and named. With none, it does nothing.
