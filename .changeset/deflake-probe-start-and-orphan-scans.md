---
"tt-project": patch
---

`tt-project`: a wait probe stopped before its start finished is now seen as ended, so its task never waits on it forever. The remote orphaned-check scan stops processes in a removed `ttp-check.*` folder only under the temp folder it scans, not anywhere on the host. `setup-box`'s IRD end-to-end script is shellcheck-clean. Seven flaky runtime tests now wait on real conditions and touch only their own processes.
