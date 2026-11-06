---
"tt-project": patch
---

`tt-project`: a persistent coordinator session no longer asks for the 1h prompt-cache lifetime right after a 5m wake whose write is still warm (a 1h request does not read a 5m write, so it re-wrote that wake's whole history). Once a session wrote at 5m, its resumed wakes stay at 5m while that write or the session's 1h write is warm. A replay of recorded session wakes put cache spend 16% lower than before and 8% lower than always using 1h.
