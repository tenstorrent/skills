---
"tt-project": patch
---

`tt-project`: a command watcher's own failures (timeout, failed exit code with no output, `"error": true` lines) are issues under `watcher-error:<name>`, apart from its reports: no receipt rule or mute of the reports covers them, they expire as usual and the next successful run clears them, while explicit_clear receipts stay pending until acknowledged
