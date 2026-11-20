---
"tt-project": patch
---

Coordinator session mode: a resumed wake sends only the digest sections that changed since the
session's last acknowledged wake, plus its new events. The whole digest still goes to a new session
and after a daemon restart, a stateless, failed or compacted wake, or the session's ask. Stateless
turns are unchanged.
