---
"tt-project": patch
---

`tt-project`: worker commands in Claude Code's Bash sandbox can reach the network through its proxy again. The sandbox sets the proxy variables (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `FTP_PROXY`, upper and lower case) to a `localhost` host, which does not resolve inside it, so `gh` and other proxied tools failed with "error connecting to localhost". Each worker command now first rewrites a literal `localhost` proxy host to `127.0.0.1` (a script the CLI sources through `CLAUDE_ENV_FILE`, chaining any inherited one), and the run supervisor does the same to the environment it starts every provider's agent with. The scheme, credentials, port and path are kept; other hosts and `NO_PROXY` are left alone, and the values are never written to run records.
