---
"tt-project": patch
---

Check commands and `ttp sandbox` test runs can run in a CPU-quota group (a systemd user scope or a delegated cgroup v2) that tightens while `checks.busy_probe` says a live service is busy. Off by default. Where neither is available, or the command cannot join the group, the command runs as before and the reason is said once. The smoke list now names one fast test from each of the new check, stop and split-gate case files.
