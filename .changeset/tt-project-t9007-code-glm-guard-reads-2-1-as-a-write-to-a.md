---
"tt-project": patch
---

`tt-project`: sidefx: a copied or closed file descriptor (2>&1, >&2, 2>&-) is no redirect target, so the GLM guard no longer reads it as a write to a file named 1 under the last cd.
