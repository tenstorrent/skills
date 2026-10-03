---
"tt-project": patch
---

`tt-project`: `ttp say` to a project on another machine no longer drops the message when that
machine cannot be reached (ssh exits 255). The message is queued in `~/.tt-project/outbox/<project>.jsonl`
(mode 0600) and the command says so and exits 0. The queue is sent in order before the next
forwarded command to that project and on every listener reconnect. An entry is removed only after
the project confirms it, and the project skips a client id it has already stored, so each message
arrives exactly once. `ttp status <name>` shows how many messages are still queued on this machine.
