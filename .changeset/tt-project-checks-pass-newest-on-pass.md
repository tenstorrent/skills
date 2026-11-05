---
"tt-project": patch
---

`tt-project`: a passed `ttp checks --result` probe now records the on_pass of the task's newest hand-off. A later wake that hands off again on the same checks with a new on_pass no longer gets the older one recorded, or a model wake instead. Probes now count as the same wait after normalising them: spacing, quoting, the path used to call `ttp`, trailing slashes and the order of a ttp probe's `--options` no longer count as a different probe. Different commands, word boundaries or quoting that changes what a `$`-probe runs still count as different probes.
