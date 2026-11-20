---
"tt-project": patch
---

`ttp deployed <older version>` no longer reports projects on a newer release as pending: a read-only probe accepts a running release newer than the target when it contains the target commit (by git in the current checkout where it can tell, else by version order) and prints `ok: runs <newer>`. A newer release git shows lacks the commit stays pending, so a deploy check or `retry_when` for an older fix passes once it is live.
