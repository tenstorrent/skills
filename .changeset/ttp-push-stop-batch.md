---
"tt-project": patch
---

`tt-project`: a running push queue batch can be stopped cleanly with `ttp push --stop-batch <marker> --why <text>` (the coordinator or any task of the project). The batch honours the request at its next safe point, ends a running check with its child processes and records the outcome `stopped`; its entries go back to approved with no try counted and no backoff, and it is never resumed. A batch in its git push is never interrupted. Each stop is audited with its why.
