---
"tt-project": patch
---

tt-project: a new per-project setting `machine.pace_from` (`moderate`, the default and today's
behaviour, or `heavy`). On a host whose projects only run agents, `heavy` lets only heavy CPU or heavy
memory readings (plus a call or heat, as before) cut the project's machine-wide worker limit, and the
project holds itself to its own base rather than the lowest base another project set, so one
moderately paced project no longer keeps every project below its own limit.
