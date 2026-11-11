---
"tt-project": patch
---

`tt-project`:

- the host guard around check runs (`ttp checks`, push checks, batch checks) also counts the check's working tree as one of the run's own folders: a crontab line, unit file or link naming it is the run's leak, fails the run and is put back. A crontab line another process adds or removes while a check runs (such as a live series job's `@reboot` resume line) is reported, left in place and never fails the check; tests cover both, and a restore that keeps a line written after the compare.
