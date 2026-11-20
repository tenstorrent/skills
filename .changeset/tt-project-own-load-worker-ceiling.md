---
"tt-project": patch
---

`tt-project`: a project's test-worker ceiling (`checks.test_project_workers`, unset) no longer shrinks under its own suites' load: before the spare-cores term, the load one worker per test slot the project itself holds is taken out of the host's load average. Load from anything else still shrinks it as before, and the host's test pool and load guard still bound the total.
