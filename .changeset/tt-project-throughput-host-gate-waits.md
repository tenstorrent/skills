---
"tt-project": patch
---

`tt-project`: `ttp throughput` names the right limit for a checks wait. A checks lane wait event that says the host's gate held it (a wait below the project cap) now counts toward the host holder ("held by the host's free-cores gate"), and one that says "nothing (about to start)" counts against no cap. Only a wait at the project cap counts as `checks.test_project_max`, so host-held waits no longer suggest raising the project cap.
