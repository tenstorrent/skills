---
"tt-project": patch
---

Bounded ssh agent use. The bridge's per-ssh agent probe (run by every ssh that parses the bridge's Match blocks, twice per bridged host) reuses a healthy answer for 5 s, so a burst of ssh calls asks the agent once instead of once per call; a down agent is never cached. A Terminal-Bench remote engine (DOCKER_HOST=ssh://) is reached through one forwarded docker socket per host per run (one ssh login, no agent forwarding, ended with the run) instead of one ssh login per docker API call; it falls back to docker's own ssh:// when the forward does not answer. Remote checks and host load probes no longer forward the agent.
