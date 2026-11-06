---
"tt-project": patch
---

A command watcher line marked `"error": "side"` is reported but never makes its run fail, so a side check's error alone no longer queues a fix task; `"error": true` alone still does.
