---
"tt-project": patch
---

`tt-project`: `ttp config <name> KEY ''` now removes a key that `project.json` holds even when the plugin no longer knows it, such as one an older version retired. It prints the value it removed and records the removal in config provenance. The same who-may gates apply as for any removal. Setting a value on an unknown key, or removing one that is not there, is still refused as an unknown or retired key. `ttp upgrade` drops the keys the plugin has retired, starting with `coordinator.unblock_effort` (every coordinator now runs at one effort level, `coordinator.effort`), with one note line each. `coordinator.model`, `coordinator.effort` and every other key stay as they are.
