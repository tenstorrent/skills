---
"tt-project": patch
---

`tt-project`: `disk.resume_free_gb` sets the free space at which a tripped disk guard lets go, so a disk
that hovers just above the threshold keeps code tasks held until there is real room. It is never below the
guard's threshold (the smaller of `disk.min_free_pct` of the disk and `disk.min_free_gb`); unset, not a
number, at or above the disk's size, or on a machine with its own `min_free_gb`, the guard keeps resuming
at 1.2× its threshold. `ttp doctor` warns about a value that is not a number or is below
`disk.min_free_gb`. The resume point shows in the alert, status, the web app and the coordinator digest.
