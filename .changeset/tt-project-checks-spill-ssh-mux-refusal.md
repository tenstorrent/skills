---
"tt-project": patch
---

Remote and spilled checks survive ssh's shared connection refusing one more session (sshd MaxSessions): the check is tried again from the start on the same host over a connection of its own, twice with a short wait, before it counts as a host fault, and that fault names the refusal. The check-host ssh keepalive is now every 5 s (a dropped link still ends in about a minute), so a quiet test run on its own connection outlasts a proxy that cuts links idle for 15 s.
