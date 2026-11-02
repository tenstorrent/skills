---
"tt-project": patch
---

`tt-project`: checks start only while the host has free cores for them. A host-wide checks slot is taken only while the host's live free cores (busy cores read from /proc/stat over the last few seconds, the load average where it cannot be read, less one check's price for each check granted in the last 30 s) cover one check's measured cores; with no check running one always starts. Each run's test grant (PYTEST_XDIST_AUTO_NUM_WORKERS, TTP_TEST_SLOTS) is at most those free cores, at least 2. `checks.live_cores 0` turns both off.
