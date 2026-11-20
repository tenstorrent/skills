---
"tt-project": patch
---

A push batch's log now shows where its time goes before the checks: one line gives the seconds its entries took to replay (merges, conflict settling, entry checks) and its slowest entries, and a push step that waited for an automatic tt-project upgrade says how long it waited. Three repo-wide scans of 5-7 s each (the Python 3.9 walrus compile, the project-imports marker scan and the smoke-list coverage scan) leave the smoke suite and run after landing; a fast smoke-list format test and a fast upgrade-merge test keep their areas in it.
