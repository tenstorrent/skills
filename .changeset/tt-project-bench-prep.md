---
"tt-project": patch
---

`tt-project`: `ttp bench prep --since <date> --out <dir>` builds, without a model call, a replay bench from the project's own landed work, for comparing models or efforts fairly. It keeps done code and work tasks created since the date whose change landed through one push and adds or changes tests, and holds those tests out as graders. Each kept task gets a clean workspace at its base commit (one commit, no remotes, tags, reflog or later history) and the landed copy of its held-out test files. Tasks whose spec quotes the fix, whose tests need a symbol the spec never names, or whose landing range is unclear are excluded with the reason. A manifest records each task's spec, commits, size stratum and file hashes, and is itself pinned by its sha256.
