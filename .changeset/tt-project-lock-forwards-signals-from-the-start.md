---
"tt-project": patch
---

`tt-project`: `ttp lock` sets up its signal forwarding before it starts the command. A SIGTERM, SIGINT or SIGHUP that came while the command was starting used to end `ttp lock` at once, freeing the lock while the command still ran; it is now kept and passed on to the command as soon as it runs, and the lock is released only once the command has ended.
