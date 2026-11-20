---
"tt-project": patch
---

Premise checks: a "fails" (already exists, nothing to fix) answer closes the code task only when its
hand-off lists in `covered` every condition, case or branch the premise or the code spec names, each with
the file:line of the code handling it, and nothing in `uncovered`. Any condition without a cite settles
the check as holds: the code task runs and its spec names the uncovered conditions, so a check that found
code for only one case no longer drops the work. The check's prompt says so. A "fails" because a
named target is absent at the base ref (or a note path lies outside the repository) lists those targets
in `missing` instead and still closes the code task.
