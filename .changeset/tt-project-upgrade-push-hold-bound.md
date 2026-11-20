---
"tt-project": patch
---

`tt-project`: an automatic upgrade is no longer held for hours by one in-flight push. A push lock whose process is gone holds nothing; after `upgrade.push_hold_min` (default 30 minutes) a push holds the upgrade only while it is inside a step (a rebase, the checks, the push itself), so the upgrade starts between steps and never in one. This holds for `ttp push`, the push queue's batches and `ttp land` alike. While it is held, `ttp status` and the web app show the hold, its cause and since when.
