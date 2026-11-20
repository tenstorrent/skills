---
"tt-project": patch
---

`tt-project`: daemon.log rotates to daemon.log.1 once it passes 16 MiB, keeping one old copy, and `ttp logs` reads only the requested tail, continuing into daemon.log.1 when the current file is shorter.
