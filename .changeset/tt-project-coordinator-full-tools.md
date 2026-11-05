---
"tt-project": patch
---

`tt-project`: a persistent coordinator session wake now has a worker's tools by default (`coordinator.session.tools`: `full`, or `read_only` for the old set): Bash, Read, Edit, Write, Skill, ToolSearch and the web tools, under the worker's PreToolUse checks. Direct writes to the charter, memory files, `harness/project.json` and the state database are denied with the action or command to use instead; any raw `git push` is denied. A foreground command stops at `coordinator.session.tool_timeout_s` (300, at most 600); a longer or background one is denied with a pointer to `ttp detach`. Each mutating call and its exit code is logged to the run's `tool-audit.jsonl` and `progress.md`. Moving back to `full` from `read_only` needs the user's word. The stateless turn still has no tools.
