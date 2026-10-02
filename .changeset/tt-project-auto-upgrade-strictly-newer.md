---
"tt-project": patch
---

`tt-project`: the daemon starts an automatic `ttp upgrade` only for a strictly newer installed
version. The same version from another commit is still shown in `ttp status` and the web app, but is
not applied unattended. An automatic upgrade re-checks push locks just before it swaps the live
harness; if a push is in flight it leaves the harness untouched, records the try as `held` (so it is
retried, not counted as tried) and exits 75, and the daemon looks again after 5 minutes.
