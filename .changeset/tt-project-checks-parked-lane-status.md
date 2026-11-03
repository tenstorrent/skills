---
"tt-project": patch
---

`tt-project`: a `ttp checks` run waiting outside the host queue for its project's test-worker budget (or the host's test pool) now reads `waits on host test slots (project budget or pool full), outside the host queue` in `ttp checks status` and `ttp status`, instead of `a host-wide checks slot` or `no host queue (starting, or an older runtime)`. The label clears once the run queues for a host slot again or starts.
