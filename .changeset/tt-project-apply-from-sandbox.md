---
"tt-project": patch
---

`tt-project`: a template-upgrade task can finish its `ttp upgrade <project> --apply <commit>` from a worker sandbox that cannot write the harness working tree. `--apply` then asks the running daemon to make the fast-forward (checked again there: the upgrade lock, the upgrade journal, and that the commit holds the template and main), waits for its result, and restarts the daemon as before. A request the daemon does not take in time is withdrawn. A fast-forward that fails part-way puts every file it wrote back to main's version, and the error names the rerun. Outside a sandbox `--apply` works as before.
