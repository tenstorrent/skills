---
"tt-project": patch
---

`tt-project`: a priority-1 task's `ttp checks` start at once in the one urgent extra lane when its repository's lanes are full of routine runs, as long as `checks.host_max` has a free slot and live cores and memory cover one run; the load average alone no longer holds it back. Lane lines, wait events and the digest mark the urgent holder.
