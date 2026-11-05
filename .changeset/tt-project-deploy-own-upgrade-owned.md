---
"tt-project": patch
---

`tt-project`: a post-push deploy check no longer fails a project whose own automatic upgrade of the release is running. That project is pending, owned by its daemon until the upgrade's window ends (also while the upgrade restarts the daemon), instead of "unowned" with a "Deploy failed" alert. A project whose daemon stopped ticking, has no owner, or is still behind past its takeover time plus the grace still fails and alerts.
