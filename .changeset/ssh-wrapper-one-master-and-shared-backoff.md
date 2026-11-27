---
"tt-project": patch
---

ssh wrapper: calls to a host with no shared connection up now queue on one lock per host while the first opens it, so a burst never opens parallel connections to a host that limits connection starts. A start refused by the host (Exceeded MaxStartups, Connection reset, Connection closed by remote host, banner exchange timeout) writes a backoff record shared by every project on the machine; calls to that host fail at once with exit 255 and a line naming the retry time, one call tries again after 1, 2, 4, 8, then 15 minutes, and the first success clears it.
