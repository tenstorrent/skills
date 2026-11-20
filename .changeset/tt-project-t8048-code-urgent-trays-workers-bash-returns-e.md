---
"tt-project": patch
---

`tt-project`: a loopproxy chain never sources itself, and a hand-started daemon never inherits its starter's run env.
- a run of one project never starts, restarts or reinstalls another project's daemon, and an upgrade never starts a stopped daemon
- a project stopped on purpose (no service, no daemon) is left out of ttp deployed's default list, after_push verdicts, the rollout count and deploy alerts, shown once as stopped on purpose
