---
"tt-project": patch
---

`tt-project`: bridge forwards whose destination is a host name are carried by the relay (bounded name resolution) instead of a raw ssh `-R` whose lookup can freeze the session, and the bridge health check never probes a destination that does not resolve.
