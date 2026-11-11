---
"tt-project": patch
---

A host-wide checks slot whose lock is free no longer names its last holder. Every reader (queue-wait and
urgent-wait events, the checks backlog line, `ttp status`, the web app and other projects' waiter alerts)
re-probes a taken-looking slot whose record names no live holder, so a probe by another daemon never turns
a stale record into a holder. The "N running (cap M)" count shows live holders only. Each daemon tick
clears a free slot's record of a gone holder while holding that slot's lock, so a real taker is never raced.
