---
"tt-project": patch
---

`tt-project`: isolated Claude workers and reviewers keep the MCP servers listed in
`providers.claude.mcp_servers`, copied per run into an owner-only temp file that is removed when the run
ends. `ttp new` turns worker isolation on for new projects; existing projects are unchanged. Unknown names
(including servers that come from a plugin, which cannot be listed) raise a low alert and show in `ttp doctor`.
