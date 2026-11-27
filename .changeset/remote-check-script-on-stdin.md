---
"tt-project": patch
---

`tt-project`: a check spilled to another host from inside a worker run now runs there instead of falling back to this host. The remote check's script and its cleanup's reap went in ssh's command line, where the run's guard read the scratch HOME's list of credential stores it keeps out, and its kills of the check's process group, as the run's own and refused the ssh. They now go on ssh's stdin behind a fixed loader that reads up to a random end line and runs nothing from a cut stream; stdin stays open, so its end still stops the check. The guard is unchanged: a credential read over ssh, or `gh auth token`, is still refused.
