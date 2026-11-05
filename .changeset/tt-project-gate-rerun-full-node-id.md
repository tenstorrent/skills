---
"tt-project": patch
---

The tests gate reruns each failing test by its full pytest node id (a new TTP_PYTEST_SELECT_IDS for
the select plugin), so a same-named test in another file never runs and its pass can no longer make
a failing test that did not run count as flaky and get quarantined.
