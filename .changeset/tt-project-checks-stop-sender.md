---
"tt-project": patch
---

`tt-project`: a detached `ttp checks` run now names whoever stops it. It takes SIGTERM, SIGINT and SIGHUP in a thread (sigtimedwait), and each stop writes a `received` row to the project's `job-signals.jsonl` with the sender's pid, uid, command line and parent chain, plus the run's folder. It also writes a line to checks.log. The run then stops as before, with exit 128+signal. Check commands still start with these signals unblocked. A stop that arrives while a waiting run re-executes after an upgrade is kept and ends the new image.
