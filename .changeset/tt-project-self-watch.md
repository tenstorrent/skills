---
"tt-project": patch
---

`tt-project`: each daemon now runs a model-free self-watch about every 30 s: the host's load, process-creation rate, memory and disk, and its own process tree per run (processes, spawn rate, CPU, RSS). A run of the project's own that stays past a limit for 60 s (1000 new processes/s, 3000 processes, or host load at twice its cores while the run uses half of them; `selfwatch.*` settings) is stopped: TERM, then KILL to its own process groups only, one event on its task and one notify. Processes it does not own are never signalled. One self-watch line shows in `ttp status`, the web app and the coordinator's Host section.
