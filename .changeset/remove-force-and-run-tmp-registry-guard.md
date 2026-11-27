---
"tt-project": patch
---

`ttp remove <name> --force` drops only the registry row of a project on another machine whose host cannot be reached (it says what it left behind). The registry now refuses a project folder inside a run's own tmp folder unless the registry itself is inside that tmp folder, so a debug run can no longer leave a scratch project in the real registry.
