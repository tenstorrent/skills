---
"tt-project": patch
---

`tt-project`:

- the PR sweep never repeats a live dispatch on the head, idles on people only, and a skipped or unread workflow is no pass
- the dispatch dedup reads the branch tip, a first sighting is the idle baseline, only listed workflows hold CI green, and people() is one filter
- people() tells bots by type as well as by login
