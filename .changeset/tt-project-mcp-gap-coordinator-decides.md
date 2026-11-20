---
"tt-project": patch
---

`tt-project`: the project decides which connected MCP servers and claude.ai connectors isolated workers get. A connected server or connector in neither `providers.claude.mcp_servers` nor `providers.claude.mcp_withheld` queues one `mcp_gap` coordinator event naming it, and the coordinator lists it in one of the two (withholding when unsure). Until then `ttp status` and the web app show one short line naming the undecided ones, with no config instructions; once each is decided nothing shows by default, and `ttp status --verbose` shows the full state. A newly connected server raises a new event.
