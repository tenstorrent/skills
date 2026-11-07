---
"tt-project": patch
---

The test select plugin (cap probes and the tests gate's failing-id reruns) now acts only in the pytest run it was given to. It takes its TTP_PYTEST_SELECT* variables out of the environment at configure and hands them to xdist workers itself, so a test that starts its own pytest no longer has all of that inner run's tests deselected (exit 5). This had made a rerun of a test that starts pytest fail on its own, and a branch tip read as failing its checks.
