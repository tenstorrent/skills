---
"tt-project": patch
---

`tt-project`: the upstream reader spends at most two minutes per daemon tick on remote inboxes
(one minute per machine). Machines left over are read first on the next tick, so every machine is
still read once per hourly round and a slow or hung machine never holds up dispatch for long.
Machines whose projects are gone are dropped from the round. The ssh read gets no stdin, ends
ssh's options before the host, and runs its command under `sh -c`, so non-POSIX login shells work.
