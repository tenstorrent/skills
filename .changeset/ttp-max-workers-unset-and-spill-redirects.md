---
"tt-project": patch
---

`tt-project`: `ttp config <project> machine.max_workers null` (or `none`, `auto`) sets the machine-wide worker limit back to its default (half the cores) instead of failing; 0, negative, fractional and non-numeric values are refused, from the command line and from a coordinator's config_set alike. Each dispatch pass of a project with the limit on now also records its limit in force, worked out from the stored load readings, besides its set base. A check whose output redirection has a space before its target (`pwd >> /some/file`, `make 2> /x/err.log`) may now spill to a spill host like `>/x/out.log` already could; before, its target counted as a path only this machine has, so the run stayed here. Tests: the fake spill host's probe reads the same on macOS as on Linux; spill tests that need GNU find or a Linux temp folder are marked Linux-only.
