---
"tt-project": patch
---

`tt-project`: a push batch's or `ttp push`'s checks (run under a verified landing mark) now take their own landing lane past the project's lanes and cap even while the coordinator's own task-less `ttp checks` holds one, so they never wait behind task checks. One landing lane of each kind per project at a time; a coordinator run waits under the cap while a mark run holds one.
