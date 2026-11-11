---
"tt-project": patch
---

`tt-project`:

- the host guard ties a run tree written right after a redirect, pipe or list separator (`>`, `<`, `|`, `;`, `&`, `,`) to the run, and counts a removed crontab line as the run's only when it names a run tree or the run rewrote the whole table; another project's lines removed mid-run are left alone.
- a daemon tick step stopped by its time budget while a child process is starting now kills that child instead of leaving it running with no handle, and a killed process tree is frozen scan by scan first so no fork escapes the kill. The tick-step budget tests wait on the real condition instead of wall time.
- `ttp checks` logs why a check runs on this machine while the rest of its run spills (for example, it names an absolute path of this machine that a spill host or Slurm node lacks), and a run that cannot spill names each check that keeps it here and its reason.
