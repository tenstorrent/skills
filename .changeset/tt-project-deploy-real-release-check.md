---
"tt-project": patch
---

`tt-project`: after a push, a deploy check waits for a behind project's daemon until its real next release check (each daemon now records when it last checked), instead of a time derived from when the project's release drift last changed. A project whose drift settled hours ago no longer reads "resumes at <now>" and is no longer taken over host-side at the first tick while its own daemon still ticks; a check that is really late reads "overdue since <time>" and is taken over after the grace. A daemon that stopped ticking still fails at once.
