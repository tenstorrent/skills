---
"tt-project": patch
---

`tt-project`: a cancelled, failed or exhausted task's 'stop requested; N jobs still running on <resource>' line now also counts its `ttp detach --remote` jobs, so a stop is never shown as done while a job still runs on another host. The daemon's job sweep asks each such job's host whether it still runs (one ssh call per job with a 10 s timeout, at most 20 s per sweep, a host that did not answer is not asked again in that sweep) and records the answer next to the job's mirrored log, so reading the line runs no ssh. A host that cannot be reached reads 'N jobs still possibly running on <host> (host unreachable)', never stopped.
