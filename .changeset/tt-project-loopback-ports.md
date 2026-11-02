---
"tt-project": patch
---

Sandboxed Claude and chat workers reach host loopback ports (ssh loopback hosts, LocalForward ends, reverse tunnel ports, local servers): each fenced Bash command gets 127.0.0.1 forwarders through the sandbox's proxy, compound commands and scripts included. `providers.<claude|chat>.loopback_ports` adds ports or turns it off; the worker prompt says a port unreachable only from inside the sandbox is a sandbox limit, never an outage or an ask.
