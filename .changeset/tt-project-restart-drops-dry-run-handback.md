---
"tt-project": patch
---

`tt-project`: a coordinator restart of a run that an on_pass_cmd dry run had handed back now drops that hand-back too: the next run starts fresh with the restart block and the new instructions instead of resuming the dropped session.
