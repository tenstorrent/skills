---
"tt-project": patch
---

`ttp detach --stop` (and a spend cap's or an ended task's stop) also writes the job's `<name>.rc`, holding 137 (128 + SIGKILL), beside its `<name>.lost` reason, unless the job wrote its own exit code. A watcher or probe that reads only the `.rc` now sees an ended job instead of a dead one; `ttp detach --check` prints `stopped on purpose rc=137 (<why>)`, and the closed job record is `ended` with `stopped` and the why.
