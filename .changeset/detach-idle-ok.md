---
"tt-project": patch
---

`ttp detach --idle-ok "<why>"` declares a detached job's low CPU or device use by design (it waits on
a remote API or another host the sampler cannot see): the daemon raises no under-use alarm for it and
shows the reason where the job is listed. Once a worker has let an under-used job run, the same job
raises no second `job_underuse` event and pulls in no more checks, until it restarts, its request
changes or its declared reason is removed.
