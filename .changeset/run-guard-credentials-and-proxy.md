---
"tt-project": patch
---

`tt-project`: the worker Bash guard refuses a command that hands the run's own model credentials (`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`) to a container or another agent (`docker run -e`, harbor `--ae`, an env file written from them), and one that exposes the run's own proxy: a listener on a non-loopback address forwarding to its port (socat, `ssh -L/-R`, `nc -l`) or a firewall rule opening it. The message names the stored-secret route (`ttp secret env`) as the safe one. It runs through the hook, so it is the same on every provider, and it is no sudo block or sandbox. The runner exports `TTP_RUN_PROXY_PORT`.
