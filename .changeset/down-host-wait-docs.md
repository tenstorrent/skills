---
"tt-project": patch
---

Document in the tt-project skill and the guide that a wait on a down ssh host is re-probed by the
daemon (15/30/60/120 s) with one heal fix or `host_down` event per down episode, so workers write
no poll loop for it.
