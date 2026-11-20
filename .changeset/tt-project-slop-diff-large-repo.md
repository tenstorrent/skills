---
"tt-project": patch
---

`ttp slop --diff` on a small diff in a large repository now finishes in seconds: it reads blobs through one streaming `git cat-file --batch` process instead of one git call per file, takes the counted numbers from the changed files alone, and bounds the whole-tree duplicate check by a time budget, saying so when that check is partial.
