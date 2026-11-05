---
"tt-project": patch
---

`tt-project`: with `coordinator.cache_ttl` "auto", a persistent coordinator session wake (`coordinator.session.mode` on) now picks its prompt-cache lifetime from the session's own writes instead of the stateless prefix. The session mode is decided first; a resumed wake goes at 5m only right after the session's own warm 1h wake, the handover wake goes at 5m, and any other session wake (a new session, a resume after a 5m wake) goes at 1h when another wake is likely. Before, every resume after the first went at 5m, so a gap of more than 5 minutes rewrote the whole history. Stateless turns are unchanged, and session wakes no longer record a 1h entry for the stateless prefix they did not send.
