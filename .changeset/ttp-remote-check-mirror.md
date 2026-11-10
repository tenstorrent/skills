---
"tt-project": patch
---

A check on another host gets its commit from a bare mirror kept on that host, sent as an incremental git bundle of only the missing commits (seeded in resumable steps), prints copy progress, and treats a copy that overruns or stalls as the host's fault; a host without usable git gets the tree over a compressed ssh.
