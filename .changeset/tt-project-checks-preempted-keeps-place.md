---
"tt-project": patch
---

`tt-project`: on the host-wide `ttp checks` queue, a checks run preempted for an urgent run keeps its place. When a slot frees, it starts ahead of every run that queued after it at its priority or a less urgent one, whatever the project turns. Preemption now asks a run that was never preempted before over one already preempted once. A queue-wait item lists a spill host only when that host can take checks now (not paused, its lock not refused, allowed for checks). When none can, the item says so and gives the reasons.
