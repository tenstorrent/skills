---
"tt-project": patch
---

A detached `ttp push` waiting for the push lock re-executes itself with a newly installed runtime, as waiting `ttp checks` do: the same pid and marker, its run lock passed on and its wait's start kept, so `ttp push --stop` still names it and the wait's limit is not reset. A failed exec waits on with the old runtime. New cases cover the re-exec and a later run of the same task stopping its earlier run's waiting push.
