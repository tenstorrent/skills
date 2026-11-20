---
"tt-project": patch
---

`tt-project`: `ttp lock` hands the lock's open files to its command, and a small keeper holds them until the command ends (for a command that closes inherited files, as ssh does), so a SIGKILL or OOM kill of `ttp lock` no longer frees the lock while the command still runs. The slot's label names the command's pid (via "command", the `ttp lock` pid as taker_pid), so probes and holder lists show it as held by a live command, never a gone one. A process the command leaves running keeps the lock until it ends, and the holder line says so.
