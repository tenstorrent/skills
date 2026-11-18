---
"tt-project": patch
---

`ttp new` run inside a worker records which project and task made the new project (`made_by` in the registry). The peer-effects check no longer raises an alert when that task removes its own project under the account's scratch folder again. Removing a project another task made, or a project outside the scratch folder, still raises it.
