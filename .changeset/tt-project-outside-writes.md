---
"tt-project": patch
---

`tt-project`: when a worker run ends without a valid hand-off (non-zero exit or no result.json), the daemon reads its transcript (Claude Code, chat, Codex and Cursor formats) and the hook's log of rewritten commands, model-free, and lists the paths it wrote or touched outside its working directory and run dir. The task's next run (a retry or a fallback) gets a short block naming them, and the coordinator gets an `outside_writes` event naming the task, run and paths: high when a path is under the home's config or credential folders, else normal. Paths only, never a command's text or a file's contents.
