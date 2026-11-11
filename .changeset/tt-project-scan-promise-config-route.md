---
"tt-project": patch
---

`tt-project`: three small fixes.
- The scan for project tools that still enforce a changed restriction no longer hits on machine aliases alone. It never names a file deleted since it was read. Once a done task has checked an item's tools, it never queues that item again.
- Promises are read only from the sender's own words, never from the status footer the harness appends to a reply. A promise belongs to the batch task its words name, never to a cancelled one.
- A refused config change now names the real way to make it. For a key `config_set` refuses but the coordinator may set, the coordinator runs `ttp config <project> <key> <value>` in its own shell after `ttp audit why`. Both the worker refusal and the `config_set` rejection say so, so a worker sent to the coordinator no longer loops.
