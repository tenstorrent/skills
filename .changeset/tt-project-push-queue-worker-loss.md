---
"tt-project": patch
---

`tt-project`: push queue and `ttp push`/`ttp land` checks: a check whose only failures are lost test workers runs once more; lost again with every worker exiting cleanly, the batch is held as an infrastructure fault (outcome `worker_loss`, exit 75) and its approvals go back to the queue, which retries after 15 min. A crash, or a loss beside real failures, still fails as before.
