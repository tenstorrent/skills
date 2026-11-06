---
"tt-project": patch
---

`tt-project`: when an exclusive run ends while its detached jobs still run, the hold keeper now writes the hold record and its registry entry before it relabels the resource's slots. Before, a job's `ttp lock` that read the slot in between was not recognised as covered by its own kept hold and queued behind it with no end, pinning the keeper too (seen under host load). The test suite also stops, and reports as a teardown error, any process a tt-project test left running in its tmp_path (by working folder or command line).
