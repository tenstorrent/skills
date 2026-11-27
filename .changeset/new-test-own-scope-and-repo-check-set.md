---
"tt-project": patch
---

The hand-off new-test check counts only tests the task's own commits add. A task that continues an accepted head, or merges the base branch in, is no longer sent back for tests that are unchanged in that head or in merged main; a test it adds itself still needs a line showing it fails on the base. A detached `ttp checks` run now records the check plan of its own repository, so editing another repository's `delivery.push_checks_by_repo` entry no longer stops it; editing its own entry, or the default `delivery.push_checks` it falls back to, still does.
