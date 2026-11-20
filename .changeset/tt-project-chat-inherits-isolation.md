---
"tt-project": patch
---

`tt-project`: chat-provider workers (Claude Code on an OpenAI-compatible endpoint) now get the same isolation as claude workers. While `providers.chat.worker_isolation` or `providers.chat.mcp_withheld` is unset it takes the `providers.claude` value, so a routed model no longer loads the user's own plugins and MCP servers (`--strict-mcp-config`, `--setting-sources project,local`) when claude workers are isolated; an explicit chat value still wins, and existing projects need no step. A connector on `mcp_withheld` is never kept by an isolated run even when `mcp_servers` lists it (each listed one withheld: the run stays strict). The coordinator may set `providers.chat.worker_isolation`, and `ttp doctor` prints worker isolation per provider.
