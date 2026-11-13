---
"tt-project": patch
---

`tt-project`:

- a batch takes members whose device or machine resources the lead holds (exclusive tasks never join or lead) and runs its shared steps once: one focused test gate, one device smoke, one landing request carrying every member's commits (user msgs #2808, #2815)
- exempt the Terminal-Bench trial-slot flock file from the durable-write check (an empty lock file, never read)
- throughput judges each worker wait against the cap in force then, from its own records and the config-change log; a cap changed outside both, or a task with no record it could start (blocked, or a resource label with no dispatch hold), counts as capacity unknown, never idle; no raise is proposed without a wait that met every slot (upstream note 926f5ac63a60f859)
