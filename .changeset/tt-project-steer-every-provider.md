---
"tt-project": patch
---

`tt-project`: a coordinator update to a running task now reaches its worker on every provider. Claude Code and Codex (where its hook runs) get it after a tool call as before; on any provider, `ttp note` prints an unread update and the new `ttp steer` command prints it on demand. Every read marks it read (`steer.offset`) and records when (`steer.reads`). The daemon checks running workers each tick: an update still unread after `steer.ack_after_s` (default 300 s; 0 turns it off) raises one `steer_unread` event to the coordinator naming the task and the update, and the event is marked handled once the worker reads it. The worker prompt says to act on an update at once. The end-of-run missed-update check is unchanged.
