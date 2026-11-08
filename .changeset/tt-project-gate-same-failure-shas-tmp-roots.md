---
"tt-project": patch
---

`tt-project`: the tests gate now treats one failure on the base and the head as the same when its error text differs only by a pytest temp root (`pytest-of-<user>/pytest-<n>/`, with or without a parallel worker's folder) or by a commit sha a git word names (`commit 3eedf5a`). Before, such a pre-existing failure was judged "failed another way" and blocked the landing. Another message, file or exit status still blocks.
