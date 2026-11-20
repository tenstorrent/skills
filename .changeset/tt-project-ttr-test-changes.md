---
"tt-project": patch
---

`tt-project`: `ttp machines change add --test` marks a change made for a test (kept through close; listings show it). `ttp ttr` and the daily review's time-to-recover line label an incident that starts while such an entry of the same project on the same machine is open as `test`, at report time so earlier rows are labelled too, keep it out of the counts and median, p90 and max, and list it apart. A heal-sourced incident now ends no later than the check's last unhealthy sample plus its period, so a sparse check no longer stretches a short fault; rows recorded before keep their end.
