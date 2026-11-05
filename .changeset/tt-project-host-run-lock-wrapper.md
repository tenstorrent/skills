---
"tt-project": patch
---

`tt-project`: `ttp host-run -- ttp lock <resource> -- <command...>` (exactly that wrapper: one resource, no options) matches `<command...>` against `runner.host_commands` under the same rules as a bare request, so one entry covers both forms; an exact entry stays exact and an unlisted command is still refused. The daemon runs it through its own `ttp lock`, which holds the lock while the command runs, and the worker hook no longer refuses that wrapped form.
