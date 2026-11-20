---
"tt-project": patch
---

`tt-project`: a push batch no longer sinks on one failing entry. After a blamed entry, the entries after it are searched without it round after round (up to six per batch): the first passing prefix lands, and the entries no round reached go back untested with no try counted and no backoff. The push queue also asks the remote with one bounded read-only `git ls-remote` before building a batch: an unreachable remote builds no batch and counts no try, death or backoff, and the first batch starts once it answers again. A batch killed by a reboot still resumes at once.
