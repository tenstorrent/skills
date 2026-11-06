---
"tt-project": patch
---

Expected finish (`ttp eta`, the coordinator's Expected finish lines) no longer invents a time for a
task waiting on work outside the harness (a Slurm job, a detached run, another project's ack): it
shows 'no measured finish yet' instead of 'next probe plus a median run', unless the hand-off gives a
measured `eta` with `eta_basis`; a detached driver can update it with `ttp progress report --eta
<time> --eta-basis "<how measured>"`. Waits on a time or on `landed:#<id>`, and waits the daemon
already woke, keep their estimates.
