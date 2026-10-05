---
"tt-project": patch
---

`ttp upgrade` no longer wipes a project's deliberate runtime edits. A runtime file on `main` that only lacks top-level names upstream ships (for example a helper removed on purpose) is kept with its other local edits when the merged runtime still compiles and imports; the upgrade prints a warning naming the missing names and records them in the automatic upgrade's outcome and notice. It is restored from upstream, as before, when the check fails without it. Deleted, empty and unparseable files are still restored.
