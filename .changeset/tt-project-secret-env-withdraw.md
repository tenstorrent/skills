---
"tt-project": patch
---

`ttp secret env` from a sandboxed worker withdraws a request the daemon has not started within 90 s (was about 16 min, longer than a tool call), or when the call gets SIGINT or SIGTERM, so the command never runs late or twice; the daemon rechecks a request under the queue lock before it starts it.
