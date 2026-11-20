---
"tt-project": patch
---

`tt-project`: a new `delivery.channel_tags` setting (empty by default) lists release-channel tags, and `ttp push --move-tag <tag> <commit>` moves one forward under a lease on its current remote value. It is the one forced push `delivery.never_force` lets through, and only when the tag is listed, the push updates that one tag on the push remote, the lease is fresh, the move is forward and the commit is on the push branch. Every other forced push stays refused. Each move is logged to `state/channel_tags.jsonl`.
