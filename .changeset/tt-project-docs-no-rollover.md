---
"tt-project": patch
---

`tt-project`: the README, the tt-project-harness skill and the coordinator tools' docstring no longer describe a restart, reboot, full context or failed wake as starting a new coordinator session. They now say a restart or reboot resumes the same session, a full context compacts in place, a failed wake reruns in the same session, and only a session the provider rejects or has expired starts a new seeded one, matching the guide.
