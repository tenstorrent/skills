---
"tt-project": patch
---

`tt-project`: an abrupt reboot no longer blocks a task for holding a lock it could not have caused it with. A power cycle the host's device broker announced (its notice file or a heal check's announcement) at the daemon's last heartbeat before the reboot now counts as a planned host cycle, so the task is requeued and its resumable jobs relaunched. A lease of another machine written with a prefix (`probe:<alias>`) is excluded like `<alias>` and `<alias>-...`.
