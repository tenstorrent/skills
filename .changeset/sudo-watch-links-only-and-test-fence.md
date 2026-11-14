---
"tt-project": patch
---

sudo watch: a root process is reported as left running by a check only when it is linked to the check itself (under one of its recorded sudo calls, or in its session), never by a command name it shares with a sudo call, so a host agent's root job that starts during a check is no longer blamed on it. The report goes to the project of the check's own folder, so a test running a check in a temp folder no longer queues an observation on the project it runs from. The plugin's own test suite puts a refusing stub sudo first on PATH and runs every test with sudo refused, so no test reaches the real sudo.
