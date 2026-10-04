---
"tt-project": patch
---

`tt-project`: shared-cluster rules for every project (worker and coordinator prompts, charter template, create guide): only the exact nodes the user listed, only nodes free and idle >= 2 h, one self-ending batch job per test with a sized time limit, no held or idle allocations, nothing left running, release never depends on a laptop or tunnel.
