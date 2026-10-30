---
"tt-project": patch
---

`tt-project` setup-box: a box's MCP callback-port wording no longer says "a second box signing in at the same time": each box a user's machine keeps a standing ssh forward to needs its own `--callback-port` and its own forward, whether or not sign-ins overlap in time, because a standing forward to the first box holds the local port and a second box's callback would either fail to bind it or reach the wrong box. agents.md, connectors.md and the `login --mcp` output (`box_agents.py`) all say this; their tests pin the new wording.
