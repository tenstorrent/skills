---
"tt-project": patch
---

`tt-project`: every `ttp upgrade` (plain, `--apply`, takeover, `--override-hold`, `--auto`) writes one line to the project's `logs/upgrade.log` at its start naming the mode, pid, parent command and the invoking run, task and project (else `terminal`). The conflict path's finish task records its real origin (`daemon`, the invoking task or run, else `cli`), never `user`. A release hold now stops the conflict path: a held project gets no finish task, a queued finish task waits at dispatch while the hold stands, and its `--apply` is refused unless `--override-hold`. `ttp said` gains `--project <name>` and `--all-projects` (read-only, labelled `<project> msg #N`), and `ttp upgrade <name> --hold` prints that project's newest user words on a freeze, demo or hold before setting it.
