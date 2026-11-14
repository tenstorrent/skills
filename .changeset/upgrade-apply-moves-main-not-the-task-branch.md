---
"tt-project": patch
---

upgrade: `ttp upgrade --apply` and `ttp upgrade` work on main itself. A clean harness checkout left on a task branch is checked out on main first; a dirty one is refused with nothing changed. The success line is printed only once main really holds the merge. The daemon puts a clean checkout left on an ended harness task's branch back on main (at start and when a harness task ends), keeps the branch, and reports a dirty one once.
