---
"tt-project": patch
---

`tt-project`: the remaining appends made outside the worker sandbox to state and run-folder files (`ttp checks` logs and detached output, `ttp lock` wait notes in progress.md, upgrade.log, restart.log, the takeover log and push logs) never follow a symlink or open a FIFO either; a refused append warns, the caller carries on, and the refusal is recorded as a handled append_refused event.
