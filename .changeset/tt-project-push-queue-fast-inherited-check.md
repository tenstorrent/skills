---
"tt-project": patch
---

`tt-project`: The push queue's check for commits inherited from a failed review no longer stalls
the daemon on large projects. Matching review specs against task branches uses a plain string
search instead of one regex per branch, which overflowed Python's pattern cache. Each review's
subject is now read once per review rather than once per task. With 600 tasks and 600 reviews,
one approval check drops from about 33 s to 0.1 s.
