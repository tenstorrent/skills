---
"tt-project": patch
---

`tt-project`: the push queue no longer silently drops an entry commit that landed rewritten when its landed copy resolved a conflict in that commit's lines. The commit is dropped only when the landing's lines in each such region account for every line the commit adds there. An amend made before its older copy landed, whose added lines the resolution lacks, is replayed: its work lands or comes back as a conflict. Each drop that relies on a resolved conflict is logged and named in the entry's result detail (`dropped_as_resolved`: commit, landed copy, files) in the batch marker.
