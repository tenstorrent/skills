---
"tt-project": patch
---

A check on another host gets its commit from a bare mirror kept on that host, sent as an incremental git bundle of only the missing commits (seeded in resumable steps), prints copy progress, and treats a copy that overruns or stalls as the host's fault; a new mirror is first filled from a clone already on the host (`delivery.checks_seed`), and no tree is sent from here unless `TTP_REMOTE_TREE_COPY=1`.
