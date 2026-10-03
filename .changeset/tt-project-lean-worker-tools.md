---
"tt-project": patch
---

Claude Code workers no longer load tools they never used: NotebookEdit, the worktree tools, ListAgents, ReportFindings, subagents (Task/Agent) and SendMessage. Each worker call's fixed context drops from about 10.6k to 7.9k tokens (measured). Bash, Read, Edit, Write, Skill, ToolSearch, Monitor and the web tools stay.
