---
"tt-project": patch
---

The machine-wide worker limit binds only when the machine is truly short: CPU idle under 15% for a full minute, memory pressure warn or critical or growing swap, a call while CPU idle is under 30%, or thermal pressure serious or worse. The load average, short spikes, compressed memory and a call alone no longer lower it; an unreadable signal holds it at its base; a project with `machine.max_workers` unset no longer drags other projects' limit down.
