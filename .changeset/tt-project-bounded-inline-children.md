---
"tt-project": patch
---

Daemon: the children the tick waits on inline (a device runner restart's `ttp devq start`, a precheck's
`ttp checks --detach`, the first healthy tick's harness `git rev-parse`) run in a session of their own,
killed whole after under a minute, so a hung one never holds a tick step past its budget or leaves a
stray process. A device runner restart is named in `tick_step_slow`.
