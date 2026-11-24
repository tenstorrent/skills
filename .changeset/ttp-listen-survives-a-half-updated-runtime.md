---
"tt-project": patch
---

`ttp upgrade` holds a whole copy of a harness's runtime while git rewrites its files, and the harness launcher runs that copy while its holder lives, so a command started mid-upgrade never runs new code beside a package missing its new module. `ttp listen` never dies on a half-updated runtime: a line it cannot render goes out as plain text, and after an import failure, or once the runtime on disk changed and settled, it re-executes its own launcher with the same arguments as the same process, going on after the last message it printed, so no message is lost or repeated.
