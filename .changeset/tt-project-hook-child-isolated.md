---
"tt-project": patch
---

`tt-project`: the harness hook (Claude Code, Codex and the coordinator session) and the stream guard's check now start Python in isolated mode with the runtime path given explicitly. A `ttp/` package, `json.py` or `PYTHONPATH` an agent sets up in its worktree can no longer replace the guard's code and turn its checks off.
