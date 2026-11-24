---
"tt-project": patch
---

`tt-project`: `ttp push --retire` counts the project's own single-push receipt (own, status `pushed`, its target naming the branch) as its push record, read by the same rule as every other push receipt; failed, unowned and other-target receipts still refuse. The bridge watcher keeps a port's owed restart marked when the restart is refused, so a later pass still restarts it. The GLM arm's database rule reads a python heredoc's code, not its comments or the multi-line literals it writes into a file; a one-line literal naming the database still refuses.
