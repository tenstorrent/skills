---
"tt-project": patch
---

`tt-project`:

- one cleanup removes leftover remote `ttp-check.*` folders: the spill probe's second, 60-minute removal is gone. The remote run script writes its own pid in its folder first (`.git/ttp-run.pid`); while that pid lives, the spill probe and a host's own pool count treat the folder as a running check and never remove it, even when every process of the run works outside the folder. Spill tests no longer depend on the spill state of a `ttp checks` run they run inside.
