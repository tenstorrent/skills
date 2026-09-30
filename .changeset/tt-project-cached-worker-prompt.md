---
"tt-project": patch
---

`tt-project`: Claude workers get the stable part of their prompt (rules, charter, memory) as an
appended system prompt, so it is read from the cache across workers (measured: 5.7k fewer
first-turn cache-write tokens per worker). New opt-in `providers.claude.worker_isolation` starts
workers without the user's own MCP servers, plugins, hooks and user settings.
