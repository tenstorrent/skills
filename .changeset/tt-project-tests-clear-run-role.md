---
"tt-project": patch
---

The test suite no longer inherits the role of the run that runs it. Tests already cleared the outer run's task; they now also clear `TTP_ROLE` and `TTP_COORD_FULL_TOOLS`. When a coordinator ran the suite, its role made the harness git wrapper refuse every push a test made, so the leak-scan push tests failed. A new test checks that the role is cleared and that a clean push still goes through the wrapper. The required-suite failure on the base was already fixed by the earlier change that runs the test's already-pushed setup push outside a run.
