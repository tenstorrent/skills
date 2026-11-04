---
"tt-project": patch
---

A failed `ttp upgrade --apply` fast-forward now keeps uncommitted local edits that were in the harness before it. Before, its cleanup put every file the upgrade touched back to main's version, so when git refused the fast-forward because local changes would be overwritten, those changes were silently lost. It now puts back only the files that matched main before the merge.
