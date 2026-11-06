---
"tt-project": patch
---

Throughput books each checks wait to where the run really waited. A `ttp checks` run now logs how long
it waited for its own lanes over every bounded wait and re-execution (lane_s), and how much of that the
host-wide test pool held (pool_s), so a wait whose lane time was re-stamped by bounded waits, or held by
the host pool, is booked to the host's free-cores gate or to other projects, never to checks.slots, and
no raise-checks.slots task opens for it. A run preempted by an urgent run logs its rerun's wait as cut by
the preempting project, and throughput books it there.
