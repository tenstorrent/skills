---
"tt-project": patch
---

`tt-project`: a task's targeted `ttp checks` now also runs the tests that call a changed case-file helper. Case files share one namespace, so a helper changed in one case file now selects every case file and test that names it, not only its own file. A module-level change to a case file runs the full check. A top-level import that is removed or changed in a case file, test file or mapped module also runs the full check, because another function may still use it. A pure import addition stays targeted.
