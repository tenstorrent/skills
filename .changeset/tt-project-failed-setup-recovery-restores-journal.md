---
"tt-project": patch
---

`tt-project`: a setup recovery whose copy or rollback fails (a full disk, a vanished plugin folder) puts the setup journal's original pid and boot back before it re-raises, so a long-lived process such as `ttp daemon` no longer leaves the journal naming itself and the next `ttp` command retries the recovery.
