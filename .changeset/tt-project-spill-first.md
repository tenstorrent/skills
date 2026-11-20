---
"tt-project": patch
---

Checks spill first: a run that may spill tries the verified spill hosts before it takes a place on the project
host (`delivery.checks_spill_first`, default true), and a run the load guard or a cap holds here is offered to
them again at each bound. A spilled run's test workers are sized to that host's idle cores (cores less its
1-minute load, within one run's limit). A check that reads git history (`git diff <base>`, `git log`) stays
on the project host, since a spill host's copy has one commit.
