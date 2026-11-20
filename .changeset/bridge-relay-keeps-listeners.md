---
"tt-project": patch
---

The access bridge's relay no longer stays alive with its listeners gone or runs out of descriptors after a sleep or network change. An accept that fails for lack of descriptors or memory is retried with a short backoff instead of ending the accept loop, and the relay keeps every listening socket referenced. Upstream connections carry TCP keepalive (idle 60 s, 4 probes 15 s apart), and once one direction of a carried connection ends the other gets 30 s before both sockets are closed. A supervisor checks each listener every 30 s (accept loop alive, socket open, socket file the one it bound) and re-listens at once; two failed re-listens in a row exit non-zero so the service restarts the relay. The relay raises its soft open-file limit to min(hard, 4096) at start.
