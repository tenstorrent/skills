---
"tt-project": patch
---

`tt-project`: full checks that all run on another host (spilled runs) no longer queue one at a time per repository. Their checks lane was taken with a count of one, so runs of different heads waited behind each other although `checks.slots` and `checks.test_project_max` had room. Such runs now go side by side up to `checks.test_project_max`, and the waiting line names that setting as the bound that holds.
