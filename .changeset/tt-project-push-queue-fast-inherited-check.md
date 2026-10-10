---
"tt-project": patch
---

`tt-project`: The push queue's check for commits inherited from a failed review no longer stalls
the daemon on large projects. Task branches are indexed once per check, so each review's spec is
scanned once instead of once per task, with a plain string search instead of one regex per branch,
which overflowed Python's pattern cache. Each review's subject is read once per review. With 600
tasks and 600 reviews, one approval check drops from about 33 s to 0.05 s.
