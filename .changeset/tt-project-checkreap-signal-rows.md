---
"tt-project": patch
---

`tt-project`: every signal the check-process reaper (checkreap) sends now writes a row to the project's `job-signals.jsonl`: the pid, signal, why (the swept check run and whether it ended or its runner died, or the end of the run's own checks) and the ended process's command line. The daemon's sweep of earlier check runs was silent before, so a detached `ttp checks` run it ended left no record of who stopped it.
