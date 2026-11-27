---
"tt-project": patch
---

`ttp detach --retry N --retry-rc RC[,RC...] [--retry-delay S]`: a step that fails with a listed exit code retries inside the same detached job (local or `--remote`, under `--lock` with the lock held across tries) instead of ending it and costing a new worker run. Each try's exit code and time go to the job's log; the `.rc` is written once from the last try. The worker prompt tells workers to wrap steps known to fail now and then this way.
