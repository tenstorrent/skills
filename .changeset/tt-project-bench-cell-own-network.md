---
"tt-project": patch
---

`tt-project`:

- bench cells (`ttp bench run`, `ttp bench daily`, replay, the device shadow) run the agent and every command it runs in their own empty network namespace: loopback only, so a command cannot reach a service on the host's loopback by socket, urllib or curl. The model call stays outside (the chat bridge or the claude CLI; the bridge port reaches the cell by a unix socket and an in-cell relay; a provider CLI that calls its own API gets a CONNECT proxy for that provider's API names on port 443 only). A host that cannot make the namespace runs no cell (`not_isolated`), never on the host network; the self-test opens a listener on the host and checks it is unreachable.
- `ttp bench reach HOST:PORT ...`: builds cells as the bench and the shadow do, tries a TCP connect to each target from inside and exits 0 only if every one is unreachable
- bench and device-shadow arms may use `provider: claude` on `mini-swe-agent` (`claude:<model>:<effort>:mini-swe-agent`): the logged-in claude CLI with the agent's own system prompt in place of Claude Code's, nothing else loaded, spend recorded per cell; each result records the CLI version and the call; tests
