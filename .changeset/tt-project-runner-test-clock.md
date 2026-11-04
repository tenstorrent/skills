---
"tt-project": patch
---

Runner: a test-only awake clock (`TTP_TEST_AWAKE_FILE`, unset in real use) lets the suite drive the run limit and stall guard; three runner tests no longer flake on a loaded host.
